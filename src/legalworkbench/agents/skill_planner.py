"""Skill Planner Agent: load contract-type review strategy, then let the LLM adjust it.

决策边界（明确的确定性/模型分工）：

- 确定性部分：技能画像的加载与合并（合同类型 -> skills -> risk_focus/top_k），
  这是企业配置，不交给模型。
- 模型决策部分：在画像基础上，模型根据条款标题分布决定是否调整检索深度
  （retrieval_top_k）和补充风险关注点（risk_focus）。决策输出必须通过校验：
  top_k 强制夹在 [5, 20]，risk_focus 只接受白名单内的风险类型——模型给方向，
  边界由代码守住。local provider 下决策退化为"不调整"，行为保持确定性。
"""

from __future__ import annotations

from legalworkbench.agents.base import LegalReviewAgent, ReviewAgentContext
from legalworkbench.governance.rules import KNOWN_RISK_TYPES

MIN_TOP_K = 5
MAX_TOP_K = 20


class SkillPlannerAgent(LegalReviewAgent):
    name = "skill_planner_agent"
    role = "review_strategy"

    def run(self, ctx: ReviewAgentContext) -> dict[str, object]:
        self.emit(ctx, "started", {"contract_type": ctx.run.contract_type})
        selected = ctx.skills.select(ctx.run.contract_type)
        ctx.run.selected_skills = [skill.name for skill in selected]
        profile = dict(ctx.skills.review_profile(ctx.run.contract_type))

        if len(ctx.run.clauses) <= 3:
            # 短合同的完整条款已经能在一次检索中覆盖，远端模型调整 top-k
            # 收益很低，却会固定增加一次网络往返。
            decision = {
                "adjust": False,
                "decision_source": "short_contract_fast_path",
                "reason": "three or fewer clauses use the configured skill profile",
            }
        else:
            decision = ctx.llm.decide(
                task="plan_review",
                payload={
                    "contract_type": ctx.run.contract_type,
                    "clause_titles": [clause.title for clause in ctx.run.clauses][:20],
                    "current_risk_focus": list(profile.get("risk_focus") or []),
                    "default_retrieval_top_k": int(profile.get("retrieval_top_k") or 10),
                    "allowed_risk_types": list(KNOWN_RISK_TYPES),
                    "instruction": (
                        "判断该合同是否需要调整检索深度或补充风险关注点。"
                        '返回 {"adjust": bool, "retrieval_top_k": int, '
                        '"extra_risk_focus": [..], "reason": str}。'
                    ),
                },
                fallback={"adjust": False},
            )
        if decision.get("adjust"):
            try:
                requested = int(decision.get("retrieval_top_k") or profile.get("retrieval_top_k") or 10)
            except (TypeError, ValueError):
                requested = int(profile.get("retrieval_top_k") or 10)
            profile["retrieval_top_k"] = max(MIN_TOP_K, min(MAX_TOP_K, requested))
            extra = decision.get("extra_risk_focus") or []
            if isinstance(extra, list):
                allowed = set(KNOWN_RISK_TYPES)
                merged = set(profile.get("risk_focus") or []) | {str(item) for item in extra if str(item) in allowed}
                profile["risk_focus"] = sorted(merged)
        ctx.run.mcp_context["skill_profile"] = profile
        ctx.run.mcp_context["llm_plan"] = decision

        self.emit(
            ctx,
            "completed",
            {
                "skills": ctx.run.selected_skills,
                "risk_focus": profile.get("risk_focus", []),
                "retrieval_top_k": profile.get("retrieval_top_k", 10),
                "llm_adjusted": bool(decision.get("adjust")),
                "decision_source": decision.get("decision_source", ""),
            },
        )
        return profile
