"""Download public contract templates and build local RAG knowledge.

Source: 国家市场监督管理总局合同示范文本库
https://htsfwb.samr.gov.cn/List
"""

from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from docx import Document


ROOT = Path(__file__).resolve().parents[1]
SOURCE_BASE = "https://htsfwb.samr.gov.cn"
LIST_API = f"{SOURCE_BASE}/api/content/SearchTemplates"
DOWNLOAD_API = f"{SOURCE_BASE}/api/File/DownTemplate"
HEADERS = {
    "User-Agent": "Mozilla/5.0 LegalAgentWorkbench/0.1",
    "Referer": f"{SOURCE_BASE}/List",
}


RISK_KEYWORDS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "payment_acceptance": ("medium", "付款与验收节点", ("付款", "支付", "价款", "验收", "发票")),
    "unlimited_liability": ("high", "赔偿责任边界", ("赔偿", "全部损失", "违约责任", "损失赔偿", "责任")),
    "data_security": ("high", "数据与信息安全", ("个人信息", "数据", "隐私", "泄露", "安全")),
    "ip_ownership": ("high", "知识产权归属", ("知识产权", "著作权", "专利", "成果", "商标")),
    "auto_renewal": ("medium", "期限与续约", ("续约", "自动续期", "服务期限", "合同期限")),
    "jurisdiction": ("medium", "争议解决与管辖", ("争议", "管辖", "法院", "仲裁")),
    "confidentiality": ("medium", "保密义务", ("保密", "商业秘密", "秘密信息")),
    "termination_notice": ("medium", "解除与终止通知", ("解除", "终止", "通知", "提前")),
    "force_majeure": ("low", "不可抗力", ("不可抗力", "不能预见", "不能避免")),
}


@dataclass
class CorpusItem:
    id: str
    title: str
    department: str
    region: str
    year: str
    is_local: bool
    type: int
    detail_url: str
    download_url: str
    raw_path: str
    markdown_path: str
    chars: int
    clauses: int
    status: str
    error: str = ""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--output", default="data/common_contracts")
    parser.add_argument("--knowledge-output", default=".lawbench/knowledge/common_contract_corpus.json")
    args = parser.parse_args()

    output = ROOT / args.output
    raw_dir = output / "raw_docx"
    md_dir = output / "markdown"
    raw_dir.mkdir(parents=True, exist_ok=True)
    md_dir.mkdir(parents=True, exist_ok=True)
    (output / "README.md").write_text(_readme(), encoding="utf-8")

    templates = fetch_templates(max(args.limit * 2, args.limit + 60))
    items: list[CorpusItem] = []
    knowledge: list[dict[str, Any]] = []
    success_count = 0
    with httpx.Client(headers=HEADERS, timeout=30.0, follow_redirects=True) as client:
        for idx, template in enumerate(templates, start=1):
            item, entries = process_template(client, template, raw_dir=raw_dir, md_dir=md_dir, index=idx)
            items.append(item)
            knowledge.extend(entries)
            if item.status == "ok":
                success_count += 1
            if success_count >= args.limit:
                break
            time.sleep(0.05)

    manifest = {
        "source": "国家市场监督管理总局合同示范文本库",
        "source_url": f"{SOURCE_BASE}/List",
        "generated_at": time.time(),
        "requested_limit": args.limit,
        "downloaded": sum(1 for item in items if item.status == "ok"),
        "failed": sum(1 for item in items if item.status != "ok"),
        "knowledge_entries": len(knowledge),
        "items": [asdict(item) for item in items],
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "CORPUS_REPORT.md").write_text(render_report(manifest), encoding="utf-8")
    knowledge_path = ROOT / args.knowledge_output
    knowledge_path.parent.mkdir(parents=True, exist_ok=True)
    knowledge_path.write_text(json.dumps(knowledge, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: manifest[k] for k in ("downloaded", "failed", "knowledge_entries")}, ensure_ascii=False, indent=2))


def fetch_templates(limit: int) -> list[dict[str, Any]]:
    templates: list[dict[str, Any]] = []
    with httpx.Client(headers=HEADERS, timeout=20.0) as client:
        for is_local in (False, True):
            page = 1
            while len(templates) < limit:
                response = client.get(LIST_API, params={"loc": str(is_local).lower(), "p": page})
                response.raise_for_status()
                payload = response.json()
                data = payload.get("Data") or []
                if not data:
                    break
                templates.extend(data)
                if page >= int(payload.get("TotalPage") or 1):
                    break
                page += 1
                time.sleep(0.05)
                if len(templates) >= limit:
                    break
    return templates[:limit]


def process_template(
    client: httpx.Client,
    template: dict[str, Any],
    *,
    raw_dir: Path,
    md_dir: Path,
    index: int,
) -> tuple[CorpusItem, list[dict[str, Any]]]:
    template_id = str(template["Id"])
    title = str(template.get("Title") or template_id)
    safe = safe_name(f"{index:03d}_{title}")
    raw_path = raw_dir / f"{safe}.docx"
    md_path = md_dir / f"{safe}.md"
    detail_url = f"{SOURCE_BASE}/View?id={template_id}"
    download_url = f"{DOWNLOAD_API}?id={quote(template_id)}&type=1"
    try:
        response = client.get(DOWNLOAD_API, params={"id": template_id, "type": 1})
        response.raise_for_status()
        raw_path.write_bytes(response.content)
        text = extract_docx_text(raw_path)
        md_path.write_text(markdown_for(template, text), encoding="utf-8")
        entries = build_knowledge_entries(template, text, detail_url)
        item = CorpusItem(
            id=template_id,
            title=title,
            department=str(template.get("Department") or ""),
            region=str(template.get("Region") or ""),
            year=str(template.get("PublishedOn") or ""),
            is_local=bool(template.get("IsLocal")),
            type=int(template.get("Type") or 0),
            detail_url=detail_url,
            download_url=download_url,
            raw_path=str(raw_path.relative_to(ROOT)),
            markdown_path=str(md_path.relative_to(ROOT)),
            chars=len(text),
            clauses=len(entries),
            status="ok",
        )
        return item, entries
    except Exception as exc:
        item = CorpusItem(
            id=template_id,
            title=title,
            department=str(template.get("Department") or ""),
            region=str(template.get("Region") or ""),
            year=str(template.get("PublishedOn") or ""),
            is_local=bool(template.get("IsLocal")),
            type=int(template.get("Type") or 0),
            detail_url=detail_url,
            download_url=download_url,
            raw_path=str(raw_path.relative_to(ROOT)),
            markdown_path=str(md_path.relative_to(ROOT)),
            chars=0,
            clauses=0,
            status="failed",
            error=str(exc),
        )
        return item, []


def extract_docx_text(path: Path) -> str:
    doc = Document(str(path))
    parts = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def build_knowledge_entries(template: dict[str, Any], text: str, detail_url: str) -> list[dict[str, Any]]:
    contract_type = infer_contract_type(str(template.get("Title") or ""))
    chunks = split_clauses(text)
    entries: list[dict[str, Any]] = []
    for idx, chunk in enumerate(chunks, start=1):
        risk_type, risk_level, clause_type = classify_clause(chunk)
        entries.append(
            {
                "id": f"samr_{template['Id']}_{idx:03d}",
                "title": f"{template.get('Title')} · {clause_title(chunk)}",
                "body": chunk[:1200],
                "contract_type": contract_type,
                "clause_type": clause_type,
                "risk_type": risk_type,
                "risk_level": risk_level,
                "source": f"samr_contract_template:{template['Id']}",
                "tags": [
                    str(template.get("Department") or ""),
                    str(template.get("Region") or ""),
                    str(template.get("PublishedOn") or ""),
                    detail_url,
                ],
            }
        )
    return entries


def split_clauses(text: str) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    chunks: list[list[str]] = []
    current: list[str] = []
    heading = re.compile(r"^(第[一二三四五六七八九十百]+条|\\d+[\\.、]|[一二三四五六七八九十]+、)")
    for line in lines:
        if heading.match(line) and current:
            chunks.append(current)
            current = [line]
        else:
            current.append(line)
        if sum(len(x) for x in current) > 900:
            chunks.append(current)
            current = []
    if current:
        chunks.append(current)
    return ["\n".join(chunk) for chunk in chunks if len("".join(chunk)) >= 30][:24]


def classify_clause(text: str) -> tuple[str, str, str]:
    for risk_type, (level, clause_type, keywords) in RISK_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            return risk_type, level, clause_type
    return "general", "low", "general"


def infer_contract_type(title: str) -> str:
    mapping = [
        ("保密", "NDA"),
        ("采购", "procurement"),
        ("买卖", "sales"),
        ("租赁", "lease"),
        ("服务", "service"),
        ("能源", "service"),
        ("物业", "service"),
        ("建设", "construction"),
        ("旅游", "consumer"),
        ("养老", "consumer"),
        ("劳动", "employment"),
        ("培训", "consumer"),
        ("预付", "consumer"),
    ]
    for keyword, value in mapping:
        if keyword in title:
            return value
    return "general"


def clause_title(text: str) -> str:
    first = text.splitlines()[0].strip()
    return first[:60] or "合同条款"


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.\\-\\u4e00-\\u9fff]+", "_", value).strip("._")[:120] or "contract"


def markdown_for(template: dict[str, Any], text: str) -> str:
    return "\n".join(
        [
            f"# {template.get('Title')}",
            "",
            f"- 来源：国家市场监督管理总局合同示范文本库",
            f"- ID：`{template.get('Id')}`",
            f"- 部门：{template.get('Department') or ''}",
            f"- 地区：{template.get('Region') or ''}",
            f"- 年份：{template.get('PublishedOn') or ''}",
            f"- 链接：{SOURCE_BASE}/View?id={template.get('Id')}",
            "",
            "## 正文",
            "",
            text,
            "",
        ]
    )


def render_report(manifest: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Common Contract Corpus Report",
            "",
            f"- Source: {manifest['source']}",
            f"- Source URL: {manifest['source_url']}",
            f"- Downloaded: {manifest['downloaded']}",
            f"- Failed: {manifest['failed']}",
            f"- Knowledge entries: {manifest['knowledge_entries']}",
            "",
            "This corpus is built from public contract model texts for local RAG and benchmark development.",
            "",
        ]
    )


def _readme() -> str:
    return """# Common Contract Corpus

This folder stores public contract model texts downloaded from 国家市场监督管理总局合同示范文本库.

- `raw_docx/`: original Word files
- `markdown/`: extracted text for inspection
- `manifest.json`: source metadata
- `CORPUS_REPORT.md`: corpus summary

Generated by `scripts/build_common_contract_corpus.py`.
"""


if __name__ == "__main__":
    main()
