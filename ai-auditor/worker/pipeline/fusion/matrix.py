"""决策融合矩阵 —— 对应详细设计 §11。

输入：规则引擎 findings + LLM 置信度（P0 阶段 LLM 未接入，置信度为 None）
输出：(处置级别, 矩阵命中依据)

P0 简化语义（与方案文档 §4.4 决策矩阵一致）：
- 任一规则 finding 为 major/critical  → REJECT（硬违规，交由路由引擎打回）
- 规则全过 + 无 LLM 信号：
    - auto_allowed=True（半自动模式内）→ PASS（自动通过）
    - 否则                             → ADVISORY（附意见，人工继续）
- LLM 接入后：高置信(≥conf_high) → PASS/ADVISORY，中置信 → ADVISORY，
  低置信(<conf_low) → ADVISORY（附建议意见，继续流转）。
"""
from __future__ import annotations

from typing import Any

from app.schemas.canonical import FindingOut

HARD_SEVERITIES = {"major", "critical"}


def fuse(
    rule_findings: list[FindingOut],
    llm_confidence: float | None,
    auto_allowed: bool,
    conf_high: float = 0.90,
    conf_low: float = 0.60,
) -> tuple[str, dict[str, Any]]:
    hard_hits = [f.problem_code for f in rule_findings if f.severity in HARD_SEVERITIES]
    if hard_hits:
        return "REJECT", {
            "cell": "rule_hard_violation",
            "rule_findings": hard_hits,
            "llm_confidence": llm_confidence,
        }

    if llm_confidence is None:
        if not rule_findings and auto_allowed:
            return "PASS", {"cell": "rule_pass_no_llm_auto_allowed", "llm_confidence": None}
        return "ADVISORY", {
            "cell": "rule_pass_no_llm" if not rule_findings else "rule_minor_findings",
            "llm_confidence": None,
            "rule_findings": [f.problem_code for f in rule_findings],
        }

    if llm_confidence >= conf_high:
        if not rule_findings and auto_allowed:
            return "PASS", {"cell": "rule_pass_llm_high_auto", "llm_confidence": llm_confidence}
        return "ADVISORY", {"cell": "rule_pass_llm_high", "llm_confidence": llm_confidence}
    if llm_confidence >= conf_low:
        return "ADVISORY", {"cell": "rule_pass_llm_mid", "llm_confidence": llm_confidence}
    return "ADVISORY", {"cell": "rule_pass_llm_low", "llm_confidence": llm_confidence}
