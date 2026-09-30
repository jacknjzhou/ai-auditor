"""TaskFrame / TaskRequirement —— v3.0 内核的任务边界契约（设计 §4.1/§4.2）。

对标 StaffDeck：
- TaskFrame       ≈ StaffDeck 的 TaskFrame（Planner 产出的一帧工作）
- TaskRequirement ≈ StaffDeck 的 TaskRequirement（Harness 的唯一任务边界）

与 StaffDeck 的差异：requirements/goal 由 **SOP 节点模板静态投影** 生成，
不从用户消息抽取（审核场景无会话意图歧义，见设计 §3 差异①）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

FrameKind = Literal[
    "rule_eval", "llm_verify", "knowledge_check",
    "risk_assess", "human_gate", "writeback",
]
FrameStatus = Literal[
    "queued", "running", "awaiting_human", "completed", "failed", "skipped",
]
FailurePolicy = Literal["degrade", "skip", "halt"]

VALID_KINDS: tuple[str, ...] = (
    "rule_eval", "llm_verify", "knowledge_check",
    "risk_assess", "human_gate", "writeback",
)
VALID_FAILURE_POLICIES: tuple[str, ...] = ("degrade", "skip", "halt")


@dataclass
class TaskRequirement:
    """帧任务边界：Harness 只在此边界内行动（对标 StaffDeck TaskRequirement）。

    goal                 —— 一句可验证的目标
    requirements         —— 可执行、可验证的子目标清单（不是能力名称）
    required_slots       —— 该帧所需输入槽位（从 snapshot 投影；缺失则 awaiting_human）
    completion_criteria  —— 完成判定标准（由 criteria_evaluator 校验）
    required_capabilities—— 强制能力：未全部成功执行则不得 completed
    optional_capabilities—— 可选能力白名单（可调用，但不构成完成门槛）
    knowledge_budget     —— 知识检索硬预算（默认 2 次，对齐 StaffDeck）
    failure_policy       —— degrade（降级继续）/ skip（跳过该帧）/ halt（终止 run）
    """

    goal: str
    requirements: list[str] = field(default_factory=list)
    required_slots: dict[str, Any] = field(default_factory=dict)
    completion_criteria: list[str] = field(default_factory=list)
    required_capabilities: list[str] = field(default_factory=list)
    optional_capabilities: list[str] = field(default_factory=list)
    knowledge_budget: int = 2
    failure_policy: FailurePolicy = "degrade"

    def allowed_capabilities(self) -> set[str]:
        """能力白名单 = 强制 + 可选（设计 §3 差异②：不做全目录自主发现）。"""
        return set(self.required_capabilities) | set(self.optional_capabilities)

    def validate(self) -> list[str]:
        """契约静态校验，返回错误清单（空 = 合法）。"""
        errors: list[str] = []
        if not self.goal or not self.goal.strip():
            errors.append("goal 不能为空")
        if self.failure_policy not in VALID_FAILURE_POLICIES:
            errors.append(f"failure_policy 非法: {self.failure_policy}")
        if self.knowledge_budget < 0:
            errors.append("knowledge_budget 不能为负")
        if len(set(self.required_capabilities)) != len(self.required_capabilities):
            errors.append("required_capabilities 存在重复项")
        overlap = set(self.required_capabilities) & set(self.optional_capabilities)
        if overlap:
            errors.append(f"能力同时出现在强制与可选清单: {sorted(overlap)}")
        return errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "requirements": list(self.requirements),
            "required_slots": dict(self.required_slots),
            "completion_criteria": list(self.completion_criteria),
            "required_capabilities": list(self.required_capabilities),
            "optional_capabilities": list(self.optional_capabilities),
            "knowledge_budget": self.knowledge_budget,
            "failure_policy": self.failure_policy,
        }


@dataclass
class TaskFrame:
    """Planner 产出的一帧工作（设计 §4.1）。"""

    frame_id: str
    run_id: str
    seq: int
    kind: FrameKind
    requirement: TaskRequirement
    node_id: str = ""
    depends_on: list[str] = field(default_factory=list)
    status: FrameStatus = "queued"
    outcome: dict[str, Any] | None = None

    def validate(self) -> list[str]:
        errors = list(self.requirement.validate())
        if self.kind not in VALID_KINDS:
            errors.append(f"frame kind 非法: {self.kind}")
        if not self.frame_id:
            errors.append("frame_id 不能为空")
        if not self.run_id:
            errors.append("run_id 不能为空")
        if self.seq < 0:
            errors.append("seq 不能为负")
        if self.frame_id in self.depends_on:
            errors.append("frame 不能依赖自身")
        return errors

    def ready(self, done_ids: set[str]) -> bool:
        """依赖全部完成（且非失败终止）时可运行。"""
        return all(dep in done_ids for dep in self.depends_on)

    def unresolved_deps(self, done_ids: set[str]) -> list[str]:
        return [dep for dep in self.depends_on if dep not in done_ids]

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame_id": self.frame_id,
            "run_id": self.run_id,
            "seq": self.seq,
            "kind": self.kind,
            "node_id": self.node_id,
            "status": self.status,
            "depends_on": list(self.depends_on),
            "requirement": self.requirement.to_dict(),
        }


def make_frame(
    run_id: str,
    seq: int,
    kind: FrameKind,
    *,
    goal: str,
    node_id: str = "",
    frame_id: str | None = None,
    depends_on: list[str] | None = None,
    **requirement_kwargs: Any,
) -> TaskFrame:
    """便捷构造（frame_id 缺省为 run_id:f{seq:02d}，保证稳定可复现）。"""
    frame = TaskFrame(
        frame_id=frame_id or f"{run_id}:f{seq:02d}",
        run_id=run_id,
        seq=seq,
        kind=kind,
        node_id=node_id,
        depends_on=list(depends_on or []),
        requirement=TaskRequirement(goal=goal, **requirement_kwargs),
    )
    errors = frame.validate()
    if errors:
        raise ValueError(f"TaskFrame 契约非法: {errors}")
    return frame
