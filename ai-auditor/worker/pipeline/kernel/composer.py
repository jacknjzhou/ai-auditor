"""Decision Composer —— 内核第三层（v3.0 设计 §3）：帧产出归一化。

三层分工（对标 StaffDeck：Turn Planner → Harness Agent → Response Generator）：

    AuditPlanner（确定性 SOP 展开）
        → HarnessAgent（逐帧执行：白名单 / 预算 / 协议修复）
            → DecisionComposer（帧产出归一：findings / 置信度 / 风险标记 / 证据）
                → 决策矩阵 fuse()（机器结论的最终确定性 gate，设计 §3 差异③）

Composer **只归一不决策**：不改变任何 findings 语义（逐帧原样合并，保持与 legacy
等价迁移口径一致），只补充矩阵入参之外的审计证据（降级帧 / 复用帧 / 调用数 /
失败帧 / 风险标记），并把 `composed` 事件所需的字段收敛到一处。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from app.schemas.canonical import FindingOut
from worker.pipeline.kernel.harness import FrameOutcome


@dataclass
class ComposedDecision:
    """归一化后的决策输入（fuse() 入参 + 审计证据）。"""

    findings: list[FindingOut] = field(default_factory=list)
    confidence: float | None = None
    summary: str = ""
    risk_flags: list[str] = field(default_factory=list)
    degraded: bool = False
    terminal: str | None = None
    executed_frames: list[str] = field(default_factory=list)
    reused_frames: list[str] = field(default_factory=list)
    failed_frames: list[str] = field(default_factory=list)
    n_calls: int = 0

    @property
    def evidence(self) -> dict[str, Any]:
        """并入 decision.matrix_reason 的审计证据（T4/T5 观测口径）。"""
        return {
            "terminal": self.terminal,
            "frames": len(self.executed_frames),
            "reused_frames": list(self.reused_frames),
            "degraded": self.degraded,
            "risk_flags": list(self.risk_flags),
            "n_calls": self.n_calls,
        }


class DecisionComposer:
    """帧产出 → 决策输入（幂等、无副作用）。"""

    def compose(self, outcomes: Iterable[FrameOutcome], *,
                terminal: str | None = None,
                executed_frames: Iterable[str] = (),
                reused_frames: Iterable[str] = ()) -> ComposedDecision:
        out = ComposedDecision(terminal=terminal,
                               executed_frames=list(executed_frames),
                               reused_frames=list(reused_frames))
        findings: list[FindingOut] = []
        for oc in outcomes:
            findings.extend(oc.findings)
            if oc.confidence is not None:
                out.confidence = oc.confidence
            if oc.summary:
                out.summary = oc.summary
            # risk_flags 已在 Harness._apply_finish 从 structured_result 归一
            for flag in oc.risk_flags:
                if flag not in out.risk_flags:
                    out.risk_flags.append(flag)
            if oc.degraded:
                out.degraded = True
            out.n_calls += len(oc.calls)
            if oc.status == "failed":
                out.failed_frames.append(oc.frame_id)
        out.findings = findings
        return out
