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
    heading = re.compile(r"^(#{1,4}\s+|\d+[\.、]\s*)(.+)$")
    for line in lines:
        match = heading.match(line.strip())
        if match and buf:
            clauses.append(ContractClause(clause_id=f"C{idx:03d}", title=title, text="\n".join(buf).strip()))
            idx += 1
            title = match.group(2).strip()
            buf = []
            continue
        if match:
            title = match.group(2).strip()
            continue
        if line.strip():
            buf.append(line)
    if buf:
        clauses.append(ContractClause(clause_id=f"C{idx:03d}", title=title, text="\n".join(buf).strip()))
    if not clauses and text.strip():
        clauses.append(ContractClause(clause_id="C001", title="合同正文", text=text.strip()))
    return clauses
