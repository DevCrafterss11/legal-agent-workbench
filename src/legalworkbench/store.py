"""File-backed persistence for project-local workbench data."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from legalworkbench.data import build_scaled_benchmark
from legalworkbench.fs import atomic_write_text
from legalworkbench.models import (
    BenchmarkCase,
    KnowledgeEntry,
    LegalMemory,
    LegalSkill,
    ReviewRun,
)
from legalworkbench.paths import (
    benchmark_path,
    contracts_dir,
    knowledge_dir,
    memory_path,
    runs_dir,
    settings_path,
    skills_dir,
    skills_path,
    workspace_dir,
)
from legalworkbench.privacy import mask_value
from legalworkbench.sample_data import (
    SAMPLE_BENCHMARK,
    SAMPLE_CONTRACT,
    SAMPLE_KNOWLEDGE,
    SAMPLE_MEMORY,
    SAMPLE_SETTINGS,
    SAMPLE_SKILLS,
)
from legalworkbench.secure_storage import secure_write_text

T = TypeVar("T", bound=BaseModel)


def load_model_list(path: Path, model: type[T]) -> list[T]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        return []
    return [model.model_validate(item) for item in raw]


def write_model_list(path: Path, items: list[BaseModel]) -> None:
    atomic_write_text(path, json.dumps([item.model_dump(mode="json") for item in items], ensure_ascii=False, indent=2) + "\n")


class WorkbenchStore:
    def __init__(self, cwd: str | Path | None = None) -> None:
        self.cwd = Path(cwd or Path.cwd()).resolve()

    @property
    def root(self) -> Path:
        return workspace_dir(self.cwd)

    def init_samples(self, *, force: bool = False) -> dict[str, Path]:
        knowledge_file = knowledge_dir(self.cwd) / "knowledge.json"
        contract_file = contracts_dir(self.cwd) / "sample_saas_contract.md"
        paths = {
            "knowledge": knowledge_file,
            "contract": contract_file,
            "skills": skills_path(self.cwd),
            "memory": memory_path(self.cwd),
            "benchmark": benchmark_path(self.cwd),
            "settings": settings_path(self.cwd),
        }
        if force or not knowledge_file.exists():
            write_model_list(knowledge_file, SAMPLE_KNOWLEDGE)
        if force or not contract_file.exists():
            secure_write_text(
                contract_file,
                SAMPLE_CONTRACT,
                cwd=self.cwd,
                purpose="stored-contract",
            )
        if force or not skills_path(self.cwd).exists():
            write_model_list(skills_path(self.cwd), SAMPLE_SKILLS)
        if force or not any(skills_dir(self.cwd).glob("*/SKILL.md")):
            for skill in SAMPLE_SKILLS:
                skill_path = skills_dir(self.cwd) / skill.name / "SKILL.md"
                atomic_write_text(
                    skill_path,
                    "\n".join(
                        [
                            "---",
                            f"name: {skill.name}",
                            f"contract_type: {skill.contract_type}",
                            f"report_style: {skill.report_style}",
                            f"risk_rules: {', '.join(skill.risk_rules)}",
                            "---",
                            "",
                            f"# {skill.name}",
                            "",
                            skill.description,
                            "",
                            "## Focus Clauses",
                            "",
                            *[f"- {item}" for item in skill.focus_clause_types],
                            "",
                        ]
                    ),
                )
        if force or not memory_path(self.cwd).exists():
            write_model_list(memory_path(self.cwd), SAMPLE_MEMORY)
        if force or not benchmark_path(self.cwd).exists():
            write_model_list(benchmark_path(self.cwd), SAMPLE_BENCHMARK)
        if force or not settings_path(self.cwd).exists():
            atomic_write_text(settings_path(self.cwd), json.dumps(SAMPLE_SETTINGS, ensure_ascii=False, indent=2) + "\n")
        runs_dir(self.cwd)
        return paths

    def load_knowledge(self) -> list[KnowledgeEntry]:
        entries: list[KnowledgeEntry] = []
        for path in sorted(knowledge_dir(self.cwd).glob("*.json")):
            entries.extend(load_model_list(path, KnowledgeEntry))
        return entries

    def load_skills(self) -> list[LegalSkill]:
        return load_model_list(skills_path(self.cwd), LegalSkill)

    def load_memory(self) -> list[LegalMemory]:
        return load_model_list(memory_path(self.cwd), LegalMemory)

    def save_memory(self, memories: list[LegalMemory]) -> None:
        write_model_list(memory_path(self.cwd), memories)

    def load_benchmark(self) -> list[BenchmarkCase]:
        return load_model_list(benchmark_path(self.cwd), BenchmarkCase)

    def write_scaled_benchmark(self, *, contract_cases: int = 80, risk_clauses: int = 300) -> Path:
        cases = build_scaled_benchmark(contract_cases=contract_cases, risk_clauses=risk_clauses)
        write_model_list(benchmark_path(self.cwd), cases)
        return benchmark_path(self.cwd)

    def save_run(self, run: ReviewRun) -> Path:
        path = runs_dir(self.cwd) / f"{run.review_run_id}.json"
        payload = mask_value(run.model_dump(mode="json"))
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return path

    def load_run(self, run_id: str) -> ReviewRun | None:
        path = runs_dir(self.cwd) / f"{run_id}.json"
        if not path.exists():
            return None
        return ReviewRun.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def list_runs(self, limit: int = 20) -> list[ReviewRun]:
        runs: list[ReviewRun] = []
        paths = sorted(runs_dir(self.cwd).glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in paths[:limit]:
            try:
                runs.append(ReviewRun.model_validate(json.loads(path.read_text(encoding="utf-8"))))
            except Exception:
                continue
        return runs
