"""AuditPlanner —— SOP 状态机 → TaskFrame 声明式展开（v3.0 内核设计 §5，T2）。

对标 StaffDeck Turn Planner 的职责（任务规划），但按设计 §3 差异①**确定性化**：
- 不用 LLM：审核是单事件触发，无会话意图歧义；SOP→帧的展开是纯函数；
- 条件边复用规则 DSL 求值器（同一 engine，不引入第二套 DSL）；
- 可静态求值的条件 → 确定性展开；运行时条件（如 hard_violation，依赖前帧结果）
  → 停止展开并记录 continuation，由 Planner 在前置帧完成后二次展开；
- Harness 不得重新路由（对齐 StaffDeck"仅 Router/Planner 可改变 SOP 顺序"）。

终态保留字：`__done__`（正常完结）、`__reject__`（硬违规终止）。
节点无 transitions 或到达保留字即终止。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from worker.pipeline.kernel.frames import VALID_KINDS, TaskFrame, make_frame
from worker.pipeline.rules.engine import RuleEngine

logger = logging.getLogger(__name__)

TERMINAL_DONE = "__done__"
TERMINAL_REJECT = "__reject__"
RESERVED = {TERMINAL_DONE, TERMINAL_REJECT}
RUNTIME_COND_PREFIX = "on_"  # 以 on_ 开头的命名条件 = 运行时条件（二次展开）


# ---------------------------------------------------------------------------
# SOP 模型与校验
# ---------------------------------------------------------------------------

@dataclass
class SOPNode:
    id: str
    kind: str
    instruction: str = ""
    required_capabilities: list[str] = field(default_factory=list)
    optional_capabilities: list[str] = field(default_factory=list)
    completion_criteria: list[str] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)
    knowledge_budget: int = 2
    failure_policy: str = "degrade"
    allowed_actions: list[str] = field(default_factory=list)
    transitions: list[dict[str, Any]] = field(default_factory=list)

    def next_for(self, cond_value: Any) -> str | None:
        """按条件值选取转移；cond=always 恒匹配。"""
        for tr in self.transitions:
            if tr.get("cond") == "always" or tr.get("cond") == cond_value:
                return tr.get("next")
        return None


def sop_validate(dsl: dict) -> list[str]:
    """SOP 静态校验，返回错误清单（空 = 合法）。"""
    errors: list[str] = []
    nodes = dsl.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return ["nodes 必须是非空数组"]
    ids: set[str] = set()
    for n in nodes:
        nid = n.get("id")
        if not nid:
            errors.append("存在缺少 id 的节点")
            continue
        if nid in RESERVED:
            errors.append(f"节点 id 不得使用保留字: {nid}")
        if nid in ids:
            errors.append(f"节点 id 重复: {nid}")
        ids.add(nid)
        if n.get("kind") not in VALID_KINDS:
            errors.append(f"节点 {nid} kind 非法: {n.get('kind')!r}")
        for tr in n.get("transitions") or []:
            nxt = tr.get("next")
            if not nxt:
                errors.append(f"节点 {nid} 存在缺少 next 的转移")
            elif nxt not in RESERVED and nxt not in {x.get("id") for x in nodes}:
                errors.append(f"节点 {nid} 转移指向未知节点: {nxt}")
            if "cond" not in tr:
                errors.append(f"节点 {nid} 存在缺少 cond 的转移")
    entry = dsl.get("entry")
    if not entry:
        errors.append("缺少 entry")
    elif entry not in ids:
        errors.append(f"entry 指向未知节点: {entry}")
    return errors


class AuditSOP:
    """已校验的审核 SOP（对应 FlowProfile.audit_sop 的内存形态）。"""

    def __init__(self, dsl: dict, *, validate: bool = True):
        if validate:
            errors = sop_validate(dsl)
            if errors:
                raise ValueError(f"SOP 非法: {errors}")
        self.dsl = dsl
        self.entry: str = dsl["entry"]
        self.sop_code: str = dsl.get("sop_code", "builtin")
        self.nodes: dict[str, SOPNode] = {
            n["id"]: SOPNode(
                id=n["id"], kind=n["kind"],
                instruction=n.get("instruction", ""),
                required_capabilities=list(n.get("required_capabilities") or []),
                optional_capabilities=list(n.get("optional_capabilities") or []),
                completion_criteria=list(n.get("completion_criteria") or []),
                requirements=list(n.get("requirements") or []),
                knowledge_budget=int(n.get("knowledge_budget", 2)),
                failure_policy=n.get("failure_policy", "degrade"),
                allowed_actions=list(n.get("allowed_actions") or []),
                transitions=list(n.get("transitions") or []),
            )
            for n in dsl["nodes"]
        }

    @classmethod
    def builtin(cls) -> "AuditSOP":
        """内置等价 SOP：现 S1–S4 链的状态机化（空配置零迁移承诺，设计 §5）。"""
        return cls({
            "sop_code": "builtin_equivalent",
            "entry": "intake",
            "nodes": [
                {"id": "intake", "kind": "rule_eval",
                 "instruction": "执行全部启用规则，产出确定性 findings",
                 "requirements": ["全部启用规则已求值"],
                 "required_capabilities": ["rule_query"],
                 "completion_criteria": ["规则求值完成且 findings 可用"],
                 "transitions": [{"cond": "always", "next": "material_check"}]},
                {"id": "material_check", "kind": "llm_verify",
                 "instruction": "核验材料完整性与表单-附件一致性",
                 "requirements": ["材料核验完成", "一致性核对完成"],
                 "required_capabilities": ["llm_verify"],
                 "optional_capabilities": ["knowledge_search"],
                 "failure_policy": "degrade",
                 "transitions": [
                     {"cond": "on_hard_violation", "next": TERMINAL_REJECT},
                     {"cond": "always", "next": "policy_check"}]},
                {"id": "policy_check", "kind": "knowledge_check",
                 "instruction": "检索现行制度条款并逐项核对 findings",
                 "requirements": ["制度条款检索完成"],
                 "required_capabilities": ["knowledge_search"],
                 "knowledge_budget": 3,
                 "transitions": [{"cond": "always", "next": "risk_gate"}]},
                {"id": "risk_gate", "kind": "risk_assess",
                 "instruction": "评估风险与置信度，低置信度时挂起等待实体人",
                 "requirements": ["风险评估完成"],
                 "required_capabilities": ["llm_assess"],
                 "allowed_actions": ["handoff_human", "override", "comment"],
                 "transitions": [
                     {"cond": "on_awaiting_resolved", "next": "writeback"},
                     {"cond": "always", "next": "writeback"}]},
                {"id": "writeback", "kind": "writeback",
                 "instruction": "按处置计划回写审批系统",
                 "requirements": ["回写计划已生成"],
                 "required_capabilities": ["writeback_probe"],
                 "transitions": [{"cond": "always", "next": TERMINAL_DONE}]},
            ],
        })


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------

@dataclass
class PlanResult:
    """一次展开的结果：帧队列 + 可能的续展开点。"""

    frames: list[TaskFrame] = field(default_factory=list)
    continuation: str | None = None       # 待二次展开的节点 id
    continuation_after: str | None = None # 依赖的前置 frame_id
    terminal: str | None = None           # 命中的保留字终态
    stop_reason: str = ""                 # continuation/terminal 的原因说明

    @property
    def done_ids(self) -> set[str]:
        return {f.frame_id for f in self.frames}

    def all_frame_ids(self) -> list[str]:
        return [f.frame_id for f in self.frames]


def _eval_cond(cond: Any, scope: dict, engine: RuleEngine) -> bool | None:
    """求值转移条件。

    返回 True/False（可静态判定）或 None（运行时条件/无法判定 → 二次展开）。
    cond 形态："always" | "never" | on_* 命名条件 | 规则 DSL clause dict
    """
    if cond == "always":
        return True
    if cond == "never":
        return False
    if isinstance(cond, str) and cond.startswith(RUNTIME_COND_PREFIX):
        return None  # 语义命名条件（on_hard_violation 等）→ 运行时判定
    if isinstance(cond, dict):
        try:
            return bool(engine.eval_clause(cond, scope, []))
        except Exception:  # noqa: BLE001 —— 条件求值失败按运行时条件处理
            return None
    return None


class AuditPlanner:
    """确定性 Planner：SOP → 有序 TaskFrame 队列。"""

    def __init__(self, functions: dict[str, Any] | None = None):
        self.engine = RuleEngine(functions)

    # ---- 主入口 ----
    def plan(self, sop: AuditSOP, run_id: str, scope: dict | None = None) -> PlanResult:
        """从 entry 展开确定性前缀。"""
        return self._walk(sop, run_id, sop.entry, seq_start=1,
                          depends_on=[], scope=scope or {})

    def expand(self, sop: AuditSOP, run_id: str, node_id: str, *,
               seq_start: int, depends_on: list[str],
               scope: dict | None = None) -> PlanResult:
        """二次展开：前置帧完成、运行时条件可判定后，从指定节点继续。"""
        return self._walk(sop, run_id, node_id, seq_start=seq_start,
                          depends_on=list(depends_on), scope=scope or {})

    # ---- 展开walk ----
    def _walk(self, sop: AuditSOP, run_id: str, start: str, *,
              seq_start: int, depends_on: list[str],
              scope: dict) -> PlanResult:
        result = PlanResult()
        node_id: str | None = start
        prev_deps = list(depends_on)
        seq = seq_start

        while node_id and node_id not in RESERVED:
            node = sop.nodes.get(node_id)
            if node is None:
                result.stop_reason = f"未知节点: {node_id}"
                break

            frame = make_frame(
                run_id, seq, node.kind,  # type: ignore[arg-type]
                frame_id=f"{run_id}:f{seq:02d}",
                node_id=node.id,
                goal=node.instruction or f"{node.kind}@{node.id}",
                depends_on=list(prev_deps),
                requirements=list(node.requirements),
                completion_criteria=list(node.completion_criteria),
                required_capabilities=list(node.required_capabilities),
                optional_capabilities=list(node.optional_capabilities),
                knowledge_budget=node.knowledge_budget,
                failure_policy=node.failure_policy,  # type: ignore[arg-type]
            )
            result.frames.append(frame)

            # ---- 选取下一转移 ----
            next_id: str | None = None
            decided = False
            for tr in node.transitions:
                cond = tr.get("cond")
                value = _eval_cond(cond, scope, self.engine)
                if value is True:
                    next_id = tr.get("next")
                    decided = True
                    break
                if value is None and not decided:
                    # 首个运行时条件：记录 continuation，停止本轮展开
                    result.continuation = tr.get("next")
                    result.continuation_after = frame.frame_id
                    result.stop_reason = (
                        f"节点 {node.id} 的条件 {cond!r} 依赖运行时结果，"
                        "待前置帧完成后二次展开")
                    return result
            if not decided:
                # 所有可判定条件均为 False 且无 always → 停止
                result.stop_reason = f"节点 {node.id} 无可满足的转移条件"
                return result

            prev_deps = [frame.frame_id]
            seq += 1
            node_id = next_id

        if node_id in RESERVED:
            result.terminal = node_id
        return result
