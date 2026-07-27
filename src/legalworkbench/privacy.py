"""PII detection and reversible masking for sensitive contract text.

设计目标（法务场景的隐私边界）：

- 合同原文只在本地信任边界内明文存在；跨出边界（远端 LLM、外部系统回发）前
  先做可逆脱敏：PII 替换为稳定占位符（同值同占位符），映射表只留在进程内。
- 远端 LLM 链路：脱敏后发送，模型回复中的占位符在本地回填；响应缓存存的也是
  脱敏文本，PII 永远不落 Redis。
- 识别采用确定性正则 + 校验（身份证校验码、银行卡 Luhn），以及
  靠近法务字段标签的本地姓名/地址实体识别。不调用远程模型，避免隐私层自身出境。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# 顺序敏感：身份证（18 位）先于银行卡（16-19 位数字）匹配，避免被误吞
_ID_CARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_BANK_CARD = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_PERSON_NAME = re.compile(
    r"(?:姓名|联系人|法定代表人|负责人|委托代理人|经办人|签署人|甲方代表|乙方代表)"
    r"\s*[：:]?\s*(?P<value>[\u3400-\u4dbf\u4e00-\u9fff·]{2,8})"
    r"(?![\u3400-\u4dbf\u4e00-\u9fff·])"
)
_ADDRESS = re.compile(
    r"(?:联系地址|通讯地址|注册地址|送达地址|办公地址|住所地|住址|地址)"
    r"\s*[：:]?\s*(?P<value>[^\r\n,，;；。]{5,100}?)"
    r"(?=\s*(?:联系人|手机|电话|邮箱|邮编)\s*[：:]|[\r\n,，;；。]|$)"
)


@dataclass(frozen=True)
class PiiPattern:
    pii_type: str
    regex: re.Pattern[str]
    value_group: str | int = 0


PII_PATTERNS: tuple[PiiPattern, ...] = (
    PiiPattern("person_name", _PERSON_NAME, "value"),
    PiiPattern("address", _ADDRESS, "value"),
    PiiPattern("id_card", _ID_CARD),
    PiiPattern("phone", _PHONE),
    PiiPattern("email", _EMAIL),
    PiiPattern("bank_card", _BANK_CARD),
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
    if pii_type == "person_name":
        return value not in {
            "手机",
            "电话",
            "邮箱",
            "地址",
            "姓名",
            "信息",
            "方式",
            "先生",
            "女士",
        }
    if pii_type == "address":
        return not value.startswith("[PII_")
    return True


def scan(text: str) -> dict[str, int]:
    """Count PII occurrences by type without mutating text."""

    counts: dict[str, int] = {}
    consumed: list[tuple[int, int]] = []
    for spec in PII_PATTERNS:
        for match in spec.regex.finditer(text):
            span = match.span(spec.value_group)
            if any(span[0] < end and span[1] > start for start, end in consumed):
                continue
            value = match.group(spec.value_group).strip()
            if not _accept(spec.pii_type, value):
                continue
            consumed.append(span)
            counts[spec.pii_type] = counts.get(spec.pii_type, 0) + 1
    return counts


def mask(text: str) -> MaskResult:
    """Replace PII with stable placeholders; same value maps to the same placeholder."""

    mapping: dict[str, str] = {}
    reverse: dict[tuple[str, str], str] = {}
    counts: dict[str, int] = {}
    consumed: list[tuple[int, int]] = []
    replacements: list[tuple[int, int, str]] = []
    for spec in PII_PATTERNS:
        for match in spec.regex.finditer(text):
            span = match.span(spec.value_group)
            if any(span[0] < end and span[1] > start for start, end in consumed):
                continue
            value = match.group(spec.value_group).strip()
            if not _accept(spec.pii_type, value):
                continue
            consumed.append(span)
            key = (spec.pii_type, value)
            placeholder = reverse.get(key)
            if placeholder is None:
                counts[spec.pii_type] = counts.get(spec.pii_type, 0) + 1
                placeholder = f"[PII_{spec.pii_type.upper()}_{counts[spec.pii_type]}]"
                reverse[key] = placeholder
                mapping[placeholder] = value
            replacements.append((span[0], span[1], placeholder))
    masked = text
    for start, end, placeholder in sorted(
        replacements, key=lambda item: item[0], reverse=True
    ):
        masked = masked[:start] + placeholder + masked[end:]
    return MaskResult(masked_text=masked, mapping=mapping, counts=counts)


def restore(text: str, mapping: dict[str, str]) -> str:
    for placeholder, value in mapping.items():
        text = text.replace(placeholder, value)
    return text


def mask_value(value: Any) -> Any:
    """Recursively mask PII before structured data crosses a persistence boundary."""

    if isinstance(value, str):
        return mask(value).masked_text
    if isinstance(value, dict):
        return {key: mask_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [mask_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(mask_value(item) for item in value)
    return value
