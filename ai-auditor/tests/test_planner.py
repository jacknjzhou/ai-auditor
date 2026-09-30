"""v3.0 内核 T2 测试：AuditPlanner SOP→TaskFrame 声明式展开。"""
from __future__ import annotations

import pytest

from worker.pipeline.kernel.frames import make_frame
from worker.pipeline.kernel.planner import (
    TERMINAL_DONE,
    TERMINAL_REJECT,
    AuditPlanner,
    AuditSOP,
    PlanResult,
    sop_validate,
)

RUN = "run_plan_01"


# ---------------------------------------------------------------------------
# SOP 校验
# ---------------------------------------------------------------------------

def test_sop_validate_errors():
    assert "nodes 必须是非空数组" in sop_validate({})
    bad = {
        "entry": "ghost",
        "nodes": [
            {"id": "a", "kind": "not_a_kind",
             "transitions": [{"cond": "always"}]},          # 缺 next
            {"id": "a", "kind": "rule_eval"},               # 重复 id
            {"id": "__done__", "kind": "rule_eval"},        # 保留字
        ],
    }
    errors = sop_validate(bad)
    assert any("kind 非法" in e for e in errors)
    assert any("缺少 next" in e for e in errors)
    assert any("重复" in e for e in errors)
    assert any("保留字" in e for e in errors)
    assert any("指向未知节点" in e for e in errors)
    assert any("指向未知节点: ghost" in e for e in
               [e for e in errors if "entry" in e]) or any("ghost" in e for e in errors)


def _always_chain_sop() -> AuditSOP:
    """全 always 转移的三节点链（用于确定性展开形态测试）。"""
    return AuditSOP({
        "sop_code": "chain3", "entry": "a",
        "nodes": [
            {"id": "a", "kind": "rule_eval",
             "transitions": [{"cond": "always", "next": "b"}]},
            {"id": "b", "kind": "knowledge_check",
             "transitions": [{"cond": "always", "next": "c"}]},
            {"id": "c", "kind": "writeback",
             "transitions": [{"cond": "always", "next": TERMINAL_DONE}]},
        ],
    })


def _always_chain_expansion_shape(planner: AuditPlanner, run: str) -> PlanResult:
    return planner.plan(_always_chain_sop(), run, scope={})


def test_builtin_sop_valid_and_five_nodes():
    sop = AuditSOP.builtin()
    assert sop_validate(sop.dsl) == []
    assert set(sop.nodes) == {"intake", "material_check", "policy_check",
                              "risk_gate", "writeback"}


def test_always_chain_expands_linear_frames_with_deps():
    planner = AuditPlanner()
    result = _always_chain_expansion_shape(planner, RUN)
    ids = [f.frame_id for f in result.frames]
    assert ids == [f"{RUN}:f01", f"{RUN}:f02", f"{RUN}:f03"]
    for prev, cur in zip(result.frames, result.frames[1:]):
        assert cur.depends_on == [prev.frame_id]
    assert result.frames[0].depends_on == []
    assert result.frames[0].node_id == "a"
    assert result.frames[0].kind == "rule_eval"
    assert result.terminal == TERMINAL_DONE
    assert result.continuation is None


def test_builtin_sop_stops_at_runtime_condition_with_frame_queued():
    """语义：帧先入队、转移后判——material_check 帧已存在，
    其 on_hard_violation 是运行时条件 → 在该帧后停止并记录 continuation。"""
    planner = AuditPlanner()
    result = planner.plan(AuditSOP.builtin(), RUN, scope={})
    assert [f.node_id for f in result.frames] == ["intake", "material_check"]
    assert result.frames[1].requirement.required_capabilities == ["llm_verify"]
    assert result.continuation == TERMINAL_REJECT
    assert result.continuation_after == f"{RUN}:f02"
    assert "on_hard_violation" in result.stop_reason


def test_second_pass_expand_from_continuation():
    planner = AuditPlanner()
    first = planner.plan(AuditSOP.builtin(), RUN, scope={})
    # 模拟 material_check 完成后：hard_violation 未发生 → 从 policy_check 继续
    second = planner.expand(
        AuditSOP.builtin(), RUN, "policy_check",
        seq_start=len(first.frames) + 1,
        depends_on=first.all_frame_ids(), scope={})
    assert [f.node_id for f in second.frames] == ["policy_check", "risk_gate"]
    assert second.frames[0].depends_on == first.all_frame_ids()
    assert second.frames[0].frame_id == f"{RUN}:f03"
    # risk_gate 的 on_awaiting_resolved 也是运行时条件 → 再次停在 continuation
    assert second.continuation == "writeback"
    assert second.continuation_after == f"{RUN}:f04"


def test_dsl_condition_true_branch_to_reject():
    dsl = {
        "sop_code": "branchy", "entry": "intake",
        "nodes": [
            {"id": "intake", "kind": "rule_eval",
             "transitions": [
                 {"cond": {"op": "gt", "left": "$form.amount", "right": 100000},
                  "next": TERMINAL_REJECT},
                 {"cond": "always", "next": "check"}]},
            {"id": "check", "kind": "knowledge_check",
             "transitions": [{"cond": "always", "next": TERMINAL_DONE}]},
        ],
    }
    sop = AuditSOP(dsl)
    planner = AuditPlanner()
    big = planner.plan(sop, RUN, scope={"form": {"amount": 200000}})
    assert big.terminal == TERMINAL_REJECT
    assert [f.node_id for f in big.frames] == ["intake"]

    small = planner.plan(sop, RUN, scope={"form": {"amount": 500}})
    assert small.terminal == TERMINAL_DONE
    assert [f.node_id for f in small.frames] == ["intake", "check"]


def test_dsl_condition_unevaluable_missing_var_is_deterministic_false():
    """规则引擎 fail-safe 语义：缺失变量 = 未命中 = False（确定性），不走 continuation。"""
    dsl = {
        "sop_code": "gap", "entry": "intake",
        "nodes": [
            {"id": "intake", "kind": "rule_eval",
             "transitions": [
                 {"cond": {"op": "truthy", "left": "$ctx.prev_decision"},
                  "next": "manual"},
                 {"cond": "always", "next": "auto"}]},
            {"id": "manual", "kind": "human_gate",
             "transitions": [{"cond": "always", "next": TERMINAL_DONE}]},
            {"id": "auto", "kind": "writeback",
             "transitions": [{"cond": "always", "next": TERMINAL_DONE}]},
        ],
    }
    planner = AuditPlanner()
    # scope 缺 $ctx.prev_decision → truthy=False → 确定性走 always 分支
    result = planner.plan(AuditSOP(dsl), RUN, scope={"form": {}})
    assert [f.node_id for f in result.frames] == ["intake", "auto"]
    assert result.terminal == TERMINAL_DONE
    assert result.continuation is None

    # 提供上下文后 truthy=True → 走 manual 分支
    result2 = planner.plan(AuditSOP(dsl), RUN,
                           scope={"form": {}, "ctx": {"prev_decision": "escalate"}})
    assert [f.node_id for f in result2.frames] == ["intake", "manual"]
    assert result2.terminal == TERMINAL_DONE


def test_no_satisfiable_transition_stops():
    dsl = {
        "sop_code": "dead_end", "entry": "intake",
        "nodes": [
            {"id": "intake", "kind": "rule_eval",
             "transitions": [{"cond": "never", "next": TERMINAL_DONE}]},
        ],
    }
    result = AuditPlanner().plan(AuditSOP(dsl), RUN, scope={})
    assert result.frames and result.terminal is None
    assert "无可满足" in result.stop_reason


def test_node_without_transitions_is_terminal():
    dsl = {
        "sop_code": "plain", "entry": "only",
        "nodes": [{"id": "only", "kind": "rule_eval"}],
    }
    result = AuditPlanner().plan(AuditSOP(dsl), RUN, scope={})
    assert [f.node_id for f in result.frames] == ["only"]
    assert result.terminal is None  # 无转移自然终止


def test_plan_is_pure_function_same_input_same_frames():
    planner = AuditPlanner()
    r1 = planner.plan(AuditSOP.builtin(), RUN, scope={"form": {"amount": 1}})
    r2 = planner.plan(AuditSOP.builtin(), RUN, scope={"form": {"amount": 1}})
    assert r1.all_frame_ids() == r2.all_frame_ids()
    assert r1.continuation == r2.continuation


def test_expand_preserves_seq_and_chain():
    planner = AuditPlanner()
    sop = _always_chain_sop()
    second = planner.expand(sop, RUN, "b", seq_start=5,
                            depends_on=["whatever"], scope={})
    assert [f.node_id for f in second.frames] == ["b", "c"]
    assert second.frames[0].frame_id == f"{RUN}:f05"
    assert second.frames[0].depends_on == ["whatever"]
    assert second.frames[1].depends_on == [second.frames[0].frame_id]
    assert second.terminal == TERMINAL_DONE
    assert second.frames[1].depends_on == [second.frames[0].frame_id]


def test_plan_result_defaults():
    r = PlanResult()
    assert r.frames == [] and r.done_ids == set() and r.all_frame_ids() == []
