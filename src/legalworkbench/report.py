"""Report rendering."""

from __future__ import annotations

from html import escape

from legalworkbench.models import ReviewRun


def render_markdown_report(run: ReviewRun) -> str:
    lines = [
        "# 企业法务 Agent 审查报告",
        "",
        f"- Review Run: `{run.review_run_id}`",
        f"- Status: `{run.status}`",
        f"- Contract Type: `{run.contract_type}`",
        f"- Contract Path: `{run.contract_path}`",
        f"- Skills: {', '.join(run.selected_skills) if run.selected_skills else 'none'}",
        "",
        "## Skill 审查策略",
    ]
    skill_profile = run.mcp_context.get("skill_profile") if isinstance(run.mcp_context, dict) else None
    if isinstance(skill_profile, dict) and skill_profile.get("skills"):
        lines.extend([
            f"- 适用技能：{', '.join(str(item) for item in skill_profile.get('skills', []))}",
            f"- 重点风险：{', '.join(str(item) for item in skill_profile.get('risk_focus', [])) or 'none'}",
            f"- 重点条款：{', '.join(str(item) for item in skill_profile.get('focus_clause_types', [])) or 'none'}",
            f"- RAG TopK：{skill_profile.get('retrieval_top_k', 10)}",
            f"- 报告风格：{skill_profile.get('report_style', 'concise')}",
        ])
        playbook = [str(item) for item in skill_profile.get("playbook", []) if str(item).strip()]
        if playbook:
            lines.append("- 审查步骤：" + "；".join(playbook[:5]))
    else:
        lines.append("- 未匹配到特定合同审查 Skill，按通用审查策略执行。")
    lines.extend([
        "",
        "## Agent 执行架构",
    ])
    architecture = run.mcp_context.get("agent_architecture") if isinstance(run.mcp_context, dict) else None
    if isinstance(architecture, dict):
        lines.append(f"- 模式：{architecture.get('pattern', 'unknown')}")
        lines.append(f"- 通信：{architecture.get('communication', '')}")
        lines.append(f"- 主 Agent：{architecture.get('supervisor', '')}")
        workers = architecture.get("workers", [])
        if isinstance(workers, list):
            lines.append(f"- 子 Agent：{', '.join(str(item) for item in workers)}")
        lines.append(f"- RAG 定位：{architecture.get('rag_role', '')}")
    else:
        lines.append("- 未记录 Agent 架构信息。")
    agent_steps = run.mcp_context.get("agent_steps") if isinstance(run.mcp_context, dict) else []
    if isinstance(agent_steps, list) and agent_steps:
        lines.append("- 执行步骤：" + " -> ".join(str(step.get("agent", "")) for step in agent_steps[:12] if isinstance(step, dict)))
    lines.extend([
        "",
        "## 风险摘要",
    ])
    if not run.findings:
        lines.append("- 未发现高置信风险条款。")
    for finding in run.findings:
        review = "，需人工复核" if finding.requires_human_review else ""
        blocked = f"，已拦截：{finding.block_reason}" if finding.blocked else ""
        lines.append(f"- **[{finding.risk_level}] {finding.risk_type}** ({finding.clause_id} {finding.clause_title})：{finding.summary}{review}{blocked}")
    lines.extend(["", "## 详细发现"])
    for finding in run.findings:
        lines.extend([
            f"### {finding.finding_id} · {finding.clause_title}",
            "",
            f"- 风险类型：`{finding.risk_type}`",
            f"- 风险等级：`{finding.risk_level}`",
            f"- 风险说明：{finding.summary}",
            f"- 置信度：{finding.confidence:.2f}" if finding.confidence else "- 置信度：未计算",
            f"- 来源覆盖：{finding.source_coverage:.2f}" if finding.source_coverage else "- 来源覆盖：未计算",
            f"- 修改建议：{finding.suggestion or '待补充'}",
            f"- 规则命中：{', '.join(finding.rule_hits) if finding.rule_hits else 'none'}",
            "",
            "证据来源：",
        ])
        for evidence in finding.evidence[:5]:
            rerank = f", rerank={evidence.rerank_score}" if evidence.rerank_score else ""
            lines.append(f"- `{evidence.source}` · {evidence.title} (score={evidence.score}{rerank})：{evidence.body_preview}")
        lines.append("")
    lines.extend(["## 记忆命中"])
    lines.extend([f"- `{memory.memory_id}` [{memory.type}] {memory.summary}" for memory in run.memory_hits[:8]] or ["- 无历史审查记忆命中。"])
    lines.extend(["", "## Reflection 复核"])
    if run.reflection_checks:
        for check in run.reflection_checks:
            review = "，需人工复核" if check.requires_human_review else ""
            lines.append(f"- `{check.status}` {check.target}：{check.summary}{review}")
    else:
        lines.append("- 未执行二次复核。")
    lines.extend(["", "## Compact 快照"])
    if run.compact_snapshot:
        snapshot = run.compact_snapshot
        lines.append(f"- Snapshot: `{snapshot.snapshot_id}`")
        lines.append(f"- Tokens: {snapshot.source_tokens} -> {snapshot.retained_tokens}，retention={snapshot.retention_rate}")
        lines.append(f"- Retained risks: {', '.join(snapshot.retained_risk_types) if snapshot.retained_risk_types else 'none'}")
    else:
        lines.append("- 未生成压缩快照。")
    lines.extend(["", "## 企业连接与工作流"])
    workflow = run.mcp_context.get("workflow") if isinstance(run.mcp_context, dict) else []
    if workflow:
        for step in workflow:
            lines.append(f"- {step.get('role', '')} -> `{step.get('tool', '')}`：{step.get('description', '')}")
    builtin_tools = run.mcp_context.get("builtin_tools") if isinstance(run.mcp_context, dict) else []
    mcp_tools = run.mcp_context.get("tools") if isinstance(run.mcp_context, dict) else []
    if builtin_tools or mcp_tools:
        lines.append("")
        lines.append("连接器工具：")
        for item in [*builtin_tools[:8], *mcp_tools[:8]]:
            lines.append(f"- `{item.get('server', '')}.{item.get('name', '')}`：{item.get('description', '')}")
    lines.extend(["", "## 执行指标"])
    for key, value in sorted(run.metrics.items()):
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Token 估算"])
    for key, value in sorted(run.token_usage.items()):
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## LLM 调用链路"])
    if run.llm_calls:
        for idx, call in enumerate(run.llm_calls, start=1):
            lines.append(
                f"{idx}. `{call.agent or 'runtime'} / {call.task or 'complete'}` "
                f"[{call.status}] {call.model} · {call.latency_ms}ms · "
                f"tokens={call.prompt_tokens}+{call.completion_tokens} · "
                f"cache={'hit' if call.cache_hit else 'miss'} · cost={call.estimated_cost:.8f}"
            )
    else:
        lines.append("- 本次运行没有 LLM 调用。")
    lines.extend(["", "## 工具调用链路"])
    for idx, trace in enumerate(run.tool_calls, start=1):
        lines.append(f"{idx}. `{trace.tool_name}` [{trace.status}] {trace.duration_ms}ms - {trace.output_summary or trace.input_summary}")
    return "\n".join(lines).strip() + "\n"


def render_dashboard_html(runs: list[ReviewRun]) -> str:
    rows = "\n".join(_row(run) for run in runs) or "<tr><td colspan='7'>No review runs yet.</td></tr>"
    total_risks = sum(len(run.findings) for run in runs)
    memory_hits = sum(len(run.memory_hits) for run in runs)
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>企业法务 Agent 执行工作台</title>
<style>
body{{margin:0;background:#eef2f7;color:#111827;font-family:ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
header{{background:#111827;color:white;padding:28px 44px}}main{{padding:24px 44px}}h1{{margin:0;font-size:28px}}p{{color:#cbd5e1}}
.metrics{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin-bottom:18px}}.metric{{background:white;border:1px solid #e2e8f0;border-radius:10px;padding:16px}}.metric strong{{font-size:26px;display:block}}
table{{width:100%;border-collapse:collapse;background:white;border:1px solid #e2e8f0;border-radius:10px;overflow:hidden}}th,td{{padding:12px;border-bottom:1px solid #e2e8f0;text-align:left;font-size:14px}}th{{background:#f8fafc;color:#64748b}}.badge{{border-radius:999px;padding:4px 8px;background:#dbeafe;color:#2563eb;font-weight:700;font-size:12px}}code{{color:#2563eb}}
</style></head><body>
<header><h1>企业法务 Agent 执行工作台</h1><p>ReviewRun 状态、风险数量、记忆命中、工具调用和报告产物。</p></header>
<main><section class="metrics"><div class="metric"><strong>{len(runs)}</strong>Review Runs</div><div class="metric"><strong>{total_risks}</strong>Risk Findings</div><div class="metric"><strong>{memory_hits}</strong>Memory Hits</div><div class="metric"><strong>{_avg_source(runs):.0%}</strong>Avg Source Coverage</div></section>
<table><thead><tr><th>Run</th><th>Status</th><th>Type</th><th>Risks</th><th>Memory</th><th>Tools</th><th>Report</th></tr></thead><tbody>{rows}</tbody></table></main>
</body></html>"""


def _row(run: ReviewRun) -> str:
    report = escape(run.report_path or "")
    return f"<tr><td><code>{escape(run.review_run_id)}</code></td><td><span class='badge'>{escape(run.status)}</span></td><td>{escape(run.contract_type)}</td><td>{len(run.findings)}</td><td>{len(run.memory_hits)}</td><td>{len(run.tool_calls)}</td><td>{report}</td></tr>"


def _avg_source(runs: list[ReviewRun]) -> float:
    values = [float(run.metrics.get("source_coverage", 0.0)) for run in runs if run.metrics]
    return sum(values) / max(1, len(values))
