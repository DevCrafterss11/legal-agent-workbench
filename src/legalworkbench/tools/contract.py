"""Contract parsing tool."""

from __future__ import annotations

from typing import Any

from legalworkbench.models import LegalSkill
from legalworkbench.governance import ToolAccess, ToolPolicy
from legalworkbench.parser import detect_contract_type, parse_clauses
from legalworkbench.retrieval import semantic_overlap_score
from legalworkbench.skills import SkillCatalog
from legalworkbench.tools.base import ToolContext, ToolResult


class ContractParserTool:
    name = "contract_parser"
    description = "Detect contract type and parse contract text into clauses."
    policy = ToolPolicy("contract.read", ToolAccess.READ)

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        text = str(arguments.get("text") or "")
        contract_type = detect_contract_type(text)
        if contract_type == "general":
            contract_type = infer_contract_type_from_skills(text, SkillCatalog(context.cwd).list()) or contract_type
        clauses = parse_clauses(text)
        return ToolResult(
            output={"contract_type": contract_type, "clauses": clauses},
            summary=f"{len(clauses)} clauses, type={contract_type}",
        )


def infer_contract_type_from_skills(text: str, skills: list[LegalSkill]) -> str:
    lowered = text.lower()
    scored: list[tuple[float, LegalSkill]] = []
    for skill in skills:
        contract_type = skill.contract_type.strip()
        if not contract_type or contract_type.lower() == "general":
            continue
        if contract_type.lower() in lowered:
            return contract_type
        profile_text = " ".join(
            [
                skill.name,
                skill.contract_type,
                skill.description,
                " ".join(skill.focus_clause_types),
                " ".join(skill.risk_rules),
                " ".join(skill.review_playbook),
            ]
        )
        score = semantic_overlap_score(text, profile_text)
        if score >= 0.12:
            scored.append((score, skill))
    if not scored:
        return ""
    scored.sort(key=lambda item: (item[0], item[1].priority), reverse=True)
    return scored[0][1].contract_type
