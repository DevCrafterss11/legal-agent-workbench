"""Skill selection and prompt metadata for contract review."""

from __future__ import annotations

from pathlib import Path

from legalworkbench.models import LegalSkill
from legalworkbench.paths import skills_dir
from legalworkbench.store import WorkbenchStore


class SkillCatalog:
    """Select skills by contract type and expose their operational hints."""

    def __init__(self, cwd: str | Path | None = None) -> None:
        self.store = WorkbenchStore(cwd)

    def list(self) -> list[LegalSkill]:
        loaded = self.store.load_skills()
        markdown = self._load_markdown_skills()
        known = {skill.name for skill in loaded}
        return sorted(
            [*loaded, *[skill for skill in markdown if skill.name not in known]],
            key=lambda skill: (-skill.priority, skill.name),
        )

    def select(self, contract_type: str) -> list[LegalSkill]:
        normalized = contract_type.lower()
        return [skill for skill in self.list() if skill.contract_type.lower() == normalized]

    def risk_focus(self, contract_type: str) -> set[str]:
        focus: set[str] = set()
        for skill in self.select(contract_type):
            focus.update(skill.risk_rules)
        return focus

    def focus_clause_types(self, contract_type: str) -> set[str]:
        focus: set[str] = set()
        for skill in self.select(contract_type):
            focus.update(skill.focus_clause_types)
        return focus

    def retrieval_top_k(self, contract_type: str) -> int:
        selected = self.select(contract_type)
        if not selected:
            return 10
        return max(5, min(20, max(skill.retrieval_top_k for skill in selected)))

    def report_style(self, contract_type: str) -> str:
        selected = self.select(contract_type)
        return selected[0].report_style if selected else "concise"

    def review_profile(self, contract_type: str) -> dict[str, object]:
        selected = self.select(contract_type)
        return {
            "contract_type": contract_type,
            "skills": [skill.name for skill in selected],
            "risk_focus": sorted(self.risk_focus(contract_type)),
            "focus_clause_types": sorted(self.focus_clause_types(contract_type)),
            "retrieval_top_k": self.retrieval_top_k(contract_type),
            "report_style": self.report_style(contract_type),
            "playbook": [step for skill in selected for step in skill.review_playbook],
        }

    def _load_markdown_skills(self) -> list[LegalSkill]:
        skills: list[LegalSkill] = []
        for path in sorted(skills_dir(self.store.cwd).glob("*/SKILL.md")):
            parsed = _parse_skill_markdown(path.read_text(encoding="utf-8"))
            if parsed is not None:
                skills.append(parsed)
        return skills


def _parse_skill_markdown(content: str) -> LegalSkill | None:
    if not content.startswith("---"):
        return None
    _, _, rest = content.partition("---")
    frontmatter, _, body = rest.partition("---")
    data: dict[str, str] = {}
    for line in frontmatter.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = value.strip()
    name = data.get("name")
    contract_type = data.get("contract_type")
    if not name or not contract_type:
        return None
    risk_rules = [item.strip() for item in data.get("risk_rules", "").split(",") if item.strip()]
    focus_clause_types = [
        line.lstrip("- ").strip()
        for line in body.splitlines()
        if line.strip().startswith("- ")
    ]
    description = next((line.strip() for line in body.splitlines() if line.strip() and not line.startswith("#") and not line.startswith("- ")), "")
    playbook = [
        line.lstrip("- ").strip()
        for line in body.splitlines()
        if line.strip().startswith("- ") and any(term in line.lower() for term in ("check", "review", "核", "审", "确认", "输出"))
    ]
    return LegalSkill(
        name=name,
        contract_type=contract_type,
        description=description or name,
        focus_clause_types=focus_clause_types,
        risk_rules=risk_rules,
        report_style=data.get("report_style", "concise"),
        priority=int(data.get("priority", "50") or 50),
        retrieval_top_k=int(data.get("retrieval_top_k", "10") or 10),
        review_playbook=playbook,
    )
