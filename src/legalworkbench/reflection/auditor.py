"""Reflection-style audit for risk findings and final reports."""

from __future__ import annotations

from legalworkbench.models import ReflectionCheck, ReviewRun


class ReflectionAuditor:
    """Second-pass review for unsupported claims and high-risk suggestions."""

    def review_findings(self, run: ReviewRun) -> list[ReflectionCheck]:
        checks: list[ReflectionCheck] = []
        for finding in run.findings:
            evidence_count = len(finding.evidence)
            if evidence_count == 0:
                checks.append(
                    ReflectionCheck(
                        check_id=f"ref_{finding.finding_id}_source",
                        target=finding.finding_id,
                        status="block",
                        summary="风险发现缺少来源证据，不能直接输出。",
                        recommendation="补充 RAG 来源或进入人工复核。",
                        evidence_count=evidence_count,
                        requires_human_review=True,
                    )
                )
                continue
            if finding.risk_level == "high" and not finding.requires_human_review:
                checks.append(
                    ReflectionCheck(
                        check_id=f"ref_{finding.finding_id}_high",
                        target=finding.finding_id,
                        status="warn",
                        summary="高风险条款应进入人工复核队列。",
                        recommendation="保留建议但标记为需法务确认。",
                        evidence_count=evidence_count,
                        requires_human_review=True,
                    )
                )
                continue
            if any(term in finding.suggestion for term in ("一定违法", "必然胜诉", "绝对合法")):
                checks.append(
                    ReflectionCheck(
                        check_id=f"ref_{finding.finding_id}_absolute",
                        target=finding.finding_id,
                        status="block",
                        summary="修改建议包含绝对化法律判断。",
                        recommendation="改写为基于来源和风险等级的审查建议。",
                        evidence_count=evidence_count,
                        requires_human_review=True,
                    )
                )
                continue
            checks.append(
                ReflectionCheck(
                    check_id=f"ref_{finding.finding_id}_ok",
                    target=finding.finding_id,
                    status="pass",
                    summary="风险发现具备来源证据和合规建议。",
                    evidence_count=evidence_count,
                    requires_human_review=finding.requires_human_review,
                )
            )
        return checks

    def apply(self, run: ReviewRun) -> ReviewRun:
        checks = self.review_findings(run)
        run.reflection_checks = checks
        blocked_targets = {check.target for check in checks if check.status == "block"}
        warning_targets = {check.target for check in checks if check.status == "warn"}
        for finding in run.findings:
            if finding.finding_id in blocked_targets:
                finding.blocked = True
                finding.block_reason = "blocked by reflection"
            if finding.finding_id in warning_targets:
                finding.requires_human_review = True
        return run
