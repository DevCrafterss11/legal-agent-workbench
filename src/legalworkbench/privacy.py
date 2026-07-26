"""PII detection and reversible masking for sensitive contract text.

设计目标（法务场景的隐私边界）：

- 合同原文只在本地信任边界内明文存在；跨出边界（远端 LLM、外部系统回发）前
  先做可逆脱敏：PII 替换为稳定占位符（同值同占位符），映射表只留在进程内。
- 远端 LLM 链路：脱敏后发送，模型回复中的占位符在本地回填；响应缓存存的也是
  脱敏文本，PII 永远不落 Redis。
- 识别采用确定性正则 + 校验（身份证校验码、银行卡 Luhn），不依赖模型——
  隐私拦截层自身不能有幻觉。识别不到的自由文本 PII 是已知边界，答辩时如实说明。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 顺序敏感：身份证（18 位）先于银行卡（16-19 位数字）匹配，避免被误吞
_ID_CARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_BANK_CARD = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")

PII_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("id_card", _ID_CARD),
    ("phone", _PHONE),
    ("email", _EMAIL),
    ("bank_card", _BANK_CARD),
)

_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK = "10X98765432"


@dataclass
class MaskResult:
    masked_text: str
    mapping: dict[str, str] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def has_pii(self) -> bool:
        return bool(self.mapping)


def valid_id_card(value: str) -> bool:
    if len(value) != 18 or not value[:17].isdigit():
        return False
    total = sum(int(digit) * weight for digit, weight in zip(value[:17], _ID_WEIGHTS))
    return _ID_CHECK[total % 11] == value[-1].upper()


def valid_bank_card(value: str) -> bool:
    digits = [int(ch) for ch in value][::-1]
    total = 0
    for index, digit in enumerate(digits):
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _accept(pii_type: str, value: str) -> bool:
    if pii_type == "id_card":
        return valid_id_card(value)
    if pii_type == "bank_card":
        return valid_bank_card(value)
    return True


def scan(text: str) -> dict[str, int]:
    """Count PII occurrences by type without mutating text."""

    counts: dict[str, int] = {}
    consumed: list[tuple[int, int]] = []
    for pii_type, pattern in PII_PATTERNS:
        for match in pattern.finditer(text):
            span = match.span()
            if any(span[0] < end and span[1] > start for start, end in consumed):
                continue
            if not _accept(pii_type, match.group()):
                continue
            consumed.append(span)
            counts[pii_type] = counts.get(pii_type, 0) + 1
    return counts


def mask(text: str) -> MaskResult:
    """Replace PII with stable placeholders; same value maps to the same placeholder."""

    mapping: dict[str, str] = {}
    reverse: dict[str, str] = {}
    counts: dict[str, int] = {}
    consumed: list[tuple[int, int]] = []
    replacements: list[tuple[int, int, str]] = []
    for pii_type, pattern in PII_PATTERNS:
        for match in pattern.finditer(text):
            span = match.span()
            if any(span[0] < end and span[1] > start for start, end in consumed):
                continue
            value = match.group()
            if not _accept(pii_type, value):
                continue
            consumed.append(span)
            placeholder = reverse.get(value)
            if placeholder is None:
                counts[pii_type] = counts.get(pii_type, 0) + 1
                placeholder = f"[PII_{pii_type.upper()}_{counts[pii_type]}]"
                reverse[value] = placeholder
                mapping[placeholder] = value
            replacements.append((span[0], span[1], placeholder))
    masked = text
    for start, end, placeholder in sorted(replacements, key=lambda item: item[0], reverse=True):
        masked = masked[:start] + placeholder + masked[end:]
    return MaskResult(masked_text=masked, mapping=mapping, counts=counts)


def restore(text: str, mapping: dict[str, str]) -> str:
    for placeholder, value in mapping.items():
        text = text.replace(placeholder, value)
    return text
