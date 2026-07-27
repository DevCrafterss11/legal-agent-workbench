"""Contract parser."""

from __future__ import annotations

import re

from legalworkbench.models import ContractClause


def detect_contract_type(text: str) -> str:
    lowered = text.lower()
    if "saas" in lowered or "软件服务" in text or "在线软件" in text:
        return "SaaS"
    if "采购" in text or "验收" in text:
        return "procurement"
    if "保密" in text or "confidential" in lowered or "nda" in lowered:
        return "NDA"
    if "劳动" in text or "员工" in text:
        return "employment"
    return "general"


def parse_clauses(text: str) -> list[ContractClause]:
    lines = text.splitlines()
    clauses: list[ContractClause] = []
    title = "合同正文"
    buf: list[str] = []
    idx = 1
    # 真实示范文本（市场监管总局等）普遍用“第X条 标题”作条款边界，
    # 与 markdown 标题、数字编号并列支持
    heading = re.compile(r"^(#{1,4}\s+|\d+[\.、]\s*|第[一二三四五六七八九十百千0-9０-９]{1,6}[条章]\s*)(.+)$")
    for line in lines:
        stripped = line.strip()
        match = heading.match(stripped)
        if match and buf:
            clauses.append(ContractClause(clause_id=f"C{idx:03d}", title=title, text="\n".join(buf).strip()))
            idx += 1
            title = _heading_title(match)
            buf = []
            continue
        if match:
            title = _heading_title(match)
            continue
        if line.strip():
            buf.append(line)
    if buf:
        clauses.append(ContractClause(clause_id=f"C{idx:03d}", title=title, text="\n".join(buf).strip()))
    if not clauses and text.strip():
        clauses.append(ContractClause(clause_id="C001", title="合同正文", text=text.strip()))
    return clauses


def _heading_title(match: re.Match[str]) -> str:
    marker = match.group(1).strip()
    rest = match.group(2).strip()
    if marker.startswith("第"):
        # “第X条 违约责任” → 保留条号便于人工核对
        return f"{marker} {rest}".strip()
    return rest
