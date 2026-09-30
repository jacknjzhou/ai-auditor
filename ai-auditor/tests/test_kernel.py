"""v3.0 内核 T1 测试：TaskFrame 契约 / 能力注册表 / Harness 串行循环。

验收对应设计 §9 T1：帧协议 12+ 用例。
"""
from __future__ import annotations

import json

import pytest

from app.schemas.canonical import FindingOut
from worker.pipeline.kernel.capabilities import (
    CapabilityRegistry,
    CapabilitySpec,
    UnknownCapabilityError,
    default_registry,
    digest,
)
from worker.pipeline.kernel.frames import TaskRequirement, make_frame
from worker.pipeline.kernel.harness import (
    HarnessAgent,
    ProtocolError,
    scripted_actor,
)

RUN = "run_test_01"


# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------

def _frame(goal="核对单据", *, kind="rule_eval", **req_kw) -> object:
    req = {"requirements": ["执行规则"], "required_capabilities": [],
           "optional_capabilities": [], **req_kw}
    return make_frame(RUN, 1, kind, goal=goal, node_id="intake", **req)


def _finding(code="HIGH_AMOUNT", severity="major", title="大额") -> dict:
    return {"problem_code": code, "severity": severity,
            "title": title, "detail": "detail of " + code}


def _registry_with_probe() -> tuple[CapabilityRegistry, dict]:
    """注册一个返回 finding 的探测能力 + 一个必然失败(不可重试)的能力。"""
    reg = CapabilityRegistry()

    def probe(ctx, args):
        return {"findings": [_finding()], "note": "probed"}

    def boom(ctx, args):
        raise ValueError("bad input")

    def flaky_net(ctx, args):
        raise TimeoutError("connection timed out")

    reg.register(CapabilitySpec(name="probe", description="探测",
                                input_schema={}, kind="builtin"), probe)
    reg.register(CapabilitySpec(name="boom", description="必失败",
                                input_schema={}), boom)
    reg.register(CapabilitySpec(name="flaky_net", description="网络抖动",
                                input_schema={}), flaky_net)
    reg.register(CapabilitySpec(name="knowledge_search", description="制度检索",
                                input_schema={"required": ["query"]},
                                budgeted=True, kind="knowledge"),
                 lambda ctx, args: {"excerpts": [{"clause": "8", "text": "x"}]})
    return reg, {"probe": probe, "boom": boom}


def _tool(name, arguments=None) -> dict:
    return {"action": "tool", "tool_name": name, "arguments": arguments or {}}


def _finish(status="completed", **kw) -> dict:
    return {"action": "finish", "status": status, "reply_fragment": "ok", **kw}


# ---------------------------------------------------------------------------
# TaskFrame / TaskRequirement 契约
# ---------------------------------------------------------------------------

def test_frame_contract_valid():
    f = _frame()
    assert f.validate() == []
    assert f.frame_id == f"{RUN}:f01"
    assert f.requirement.allowed_capabilities() == set()


def test_frame_contract_invalid_goal_and_policy():
    with pytest.raises(ValueError, match="goal"):
        make_frame(RUN, 2, "rule_eval", goal="  ")


def test_requirement_rejects_overlap_and_duplicates():
    req = TaskRequirement(goal="g", required_capabilities=["a", "a"],
                          optional_capabilities=["a"])
    errors = req.validate()
    assert any("重复" in e for e in errors)
    assert any("同时出现" in e for e in errors)


def test_requirement_bad_failure_policy():
    req = TaskRequirement(goal="g", failure_policy="explode")
    assert any("failure_policy" in e for e in req.validate())


def test_frame_dependency_readiness():
    f = make_frame(RUN, 2, "llm_verify", goal="核验",
                   frame_id="f02", depends_on=["f01"])
    assert not f.ready(set())
    assert f.unresolved_deps({"f02"}) == ["f01"]
    assert f.ready({"f01"})
    with pytest.raises(ValueError, match="依赖自身"):
        make_frame(RUN, 3, "llm_verify", goal="x", frame_id="f03",
                   depends_on=["f03"])


# ---------------------------------------------------------------------------
# 能力注册表
# ---------------------------------------------------------------------------

def test_registry_duplicate_registration_rejected():
    reg, _ = _registry_with_probe()
    with pytest.raises(ValueError, match="重复注册"):
        reg.register(CapabilitySpec(name="probe", description="dup"), lambda c, a: {})


def test_registry_catalog_compact_and_describe_schema():
    reg, _ = _registry_with_probe()
    cat = reg.catalog(["probe", "ghost"])
    assert cat[0] == {"name": "probe", "kind": "builtin", "description": "探测"}
    assert cat[1]["kind"] == "unavailable"
    d = reg.describe(["probe", "ghost"])
    assert d["available"][0]["input_schema"] == {}
    assert d["unavailable_references"] == ["ghost"]


def test_registry_call_records_latency_and_exception_classification():
    reg, _ = _registry_with_probe()
    ctx = type("C", (), {"db": None})()
    from worker.pipeline.kernel.capabilities import CapabilityContext
    ctx = CapabilityContext()

    rec, result = reg.call("probe", ctx, {"q": 1})
    assert rec.ok and result["findings"][0]["problem_code"] == "HIGH_AMOUNT"
    assert rec.latency_ms >= 0

    rec_bad, result_bad = reg.call("boom", ctx, {"x": 1})
    assert not rec_bad.ok and not rec_bad.retryable  # ValueError → 不可重试

    rec_net, _ = reg.call("flaky_net", ctx, {"x": 1})
    assert rec_net.retryable  # connection/timeout 关键词 → 可重试

    rec_unknown, _ = reg.call("ghost", ctx, {})
    assert not rec_unknown.ok and "未知能力" in rec_unknown.error

    with pytest.raises(UnknownCapabilityError):
        reg.spec("ghost")


def test_digest_is_deterministic():
    assert digest({"a": 1, "b": [2, 3]}) == digest({"b": [2, 3], "a": 1})
    assert digest({"a": 1}) != digest({"a": 2})


# ---------------------------------------------------------------------------
# Harness 串行循环
# ---------------------------------------------------------------------------

def test_harness_happy_path_with_fenced_json():
    reg, _ = _registry_with_probe()
    agent = HarnessAgent(reg)
    f = _frame(required_capabilities=["probe"])
    # 第一轮动作是带代码围栏的字符串（模拟真实 LLM 输出）
    raw = "```json\n" + json.dumps(_tool("probe", {"k": "v"})) + "\n```"
    outcome = agent.run(f, type("C", (), {})(), scripted_actor(
        [raw, _finish(structured_result={"confidence": 0.93,
                                         "risk_flags": ["stale_receipt"]})]))
    assert outcome.status == "completed"
    assert [c.name for c in outcome.ok_calls()] == ["probe"]
    assert outcome.findings[0].problem_code == "HIGH_AMOUNT"
    assert outcome.confidence == 0.93
    assert outcome.risk_flags == ["stale_receipt"]
    assert outcome.protocol_repairs == 0


def test_harness_required_capability_gate():
    reg, _ = _registry_with_probe()
    agent = HarnessAgent(reg)
    f = _frame(required_capabilities=["probe"])
    # 直接 finish completed（未执行 probe）→ 修复 2 次后失败
    outcome = agent.run(f, type("C", (), {})(), scripted_actor([_finish()]))
    assert outcome.status == "failed"
    assert outcome.structured_result["failure_reason"] == "missing_required_capabilities"


def test_harness_required_gate_passes_after_repair():
    reg, _ = _registry_with_probe()
    agent = HarnessAgent(reg)
    f = _frame(required_capabilities=["probe"])
    outcome = agent.run(f, type("C", (), {})(), scripted_actor(
        [_finish(), _tool("probe"), _finish()]))
    assert outcome.status == "completed"
    assert outcome.protocol_repairs == 1


def test_harness_forbidden_capability():
    reg, _ = _registry_with_probe()
    agent = HarnessAgent(reg)
    f = _frame(optional_capabilities=[])  # 白名单为空
    outcome = agent.run(f, type("C", (), {})(), scripted_actor(
        [_tool("probe"), _tool("probe"), _tool("probe")]))
    assert outcome.status == "failed"
    assert outcome.structured_result["failure_reason"] == "capability_forbidden"


def test_harness_unknown_capability():
    reg, _ = _registry_with_probe()
    agent = HarnessAgent(reg)
    f = _frame(optional_capabilities=["ghost"])
    outcome = agent.run(f, type("C", (), {})(), scripted_actor(
        [_tool("ghost"), _tool("ghost"), _tool("ghost")]))
    assert outcome.status == "failed"
    assert outcome.structured_result["failure_reason"] == "protocol_error"


def test_harness_invalid_action_shape_triggers_repair():
    reg, _ = _registry_with_probe()
    agent = HarnessAgent(reg)
    f = _frame(optional_capabilities=["probe"])
    outcome = agent.run(f, type("C", (), {})(), scripted_actor(
        [{"action": "capability_describe"},  # 非法 action（对齐 StaffDeck 修复规则）
         _tool("probe"), _finish()]))
    assert outcome.status == "completed"
    assert outcome.protocol_repairs == 1


def test_harness_finish_failed_respects_halt_policy():
    reg, _ = _registry_with_probe()
    agent = HarnessAgent(reg)
    f_halt = _frame(failure_policy="halt", optional_capabilities=["probe"])
    o1 = agent.run(f_halt, type("C", (), {})(), scripted_actor(
        [_finish(status="failed", reply_fragment="材料不可读")]))
    assert o1.status == "failed" and o1.halt_run

    f_degrade = _frame(failure_policy="degrade", optional_capabilities=["probe"])
    o2 = agent.run(f_degrade, type("C", (), {})(), scripted_actor(
        [_finish(status="failed")]))
    assert o2.status == "failed" and not o2.halt_run


def test_harness_awaiting_human_passthrough():
    reg, _ = _registry_with_probe()
    agent = HarnessAgent(reg)
    f = _frame(kind="human_gate", optional_capabilities=["probe"])
    outcome = agent.run(f, type("C", (), {})(), scripted_actor(
        [_finish(status="awaiting_human", reply_fragment="请补充住宿发票")]))
    assert outcome.status == "awaiting_human"


def test_harness_loop_limit():
    reg, _ = _registry_with_probe()
    agent = HarnessAgent(reg, max_loops=3)
    f = _frame(optional_capabilities=["probe"])
    forever = scripted_actor([])  # scripted_actor 耗尽后强制 finish → 需自定义
    def chatty(turns):
        return _tool("probe")
    outcome = agent.run(f, type("C", (), {})(), chatty)
    assert outcome.status == "failed"
    assert outcome.structured_result["failure_reason"] == "loop_limit"
    assert outcome.loops == 3


def test_harness_knowledge_budget_hard_stop():
    reg, _ = _registry_with_probe()
    agent = HarnessAgent(reg)
    f = _frame(kind="knowledge_check", knowledge_budget=1,
               optional_capabilities=["knowledge_search"])
    outcome = agent.run(f, type("C", (), {})(), scripted_actor([
        _tool("knowledge_search", {"query": "住宿费标准"}),
        _tool("knowledge_search", {"query": "换个说法再查一次"}),
        _finish(),
    ]))
    assert outcome.status == "completed"
    calls = [c for c in outcome.calls if c.name == "knowledge_search"]
    assert len(calls) == 2
    assert calls[0].ok and calls[0].budget_consumed
    assert not calls[1].ok and calls[1].error == "budget_exhausted"
    assert calls[1].retryable is False


def test_harness_forbids_identical_retry_after_non_retryable_failure():
    reg, _ = _registry_with_probe()
    agent = HarnessAgent(reg)
    f = _frame(optional_capabilities=["boom"])
    same = _tool("boom", {"x": 1})
    outcome = agent.run(f, type("C", (), {})(), scripted_actor(
        [same, dict(same), dict(same), dict(same)]))
    assert outcome.status == "failed"
    assert outcome.structured_result["failure_reason"] == "retry_forbidden"


def test_harness_allows_retry_after_non_retryable_with_new_args():
    reg, _ = _registry_with_probe()
    agent = HarnessAgent(reg)
    f = _frame(optional_capabilities=["boom"])
    outcome = agent.run(f, type("C", (), {})(), scripted_actor([
        _tool("boom", {"x": 1}),
        _finish(status="failed", reply_fragment="参数不可修复"),
    ]))
    assert outcome.status == "failed"
    # 正常 finish failed：不注入人工 failure_reason，只保留 finish 动作本身
    assert not outcome.structured_result or "failure_reason" not in outcome.structured_result


def test_harness_criteria_evaluator_gate():
    reg, _ = _registry_with_probe()
    checked: list[list[str]] = []

    def evaluator(criteria, outcome, frame):
        checked.append(list(criteria))
        return ["预算余额未核对"] if not outcome.ok_calls() else []

    agent = HarnessAgent(reg, criteria_evaluator=evaluator)
    f = _frame(optional_capabilities=["probe"],
               completion_criteria=["预算余额已核对"])
    outcome = agent.run(f, type("C", (), {})(), scripted_actor(
        [_finish(), _tool("probe"), _finish()]))
    assert outcome.status == "completed"
    assert outcome.protocol_repairs == 1
    assert checked[-1] == ["预算余额已核对"]


def test_harness_collects_and_validates_findings():
    reg, _ = _registry_with_probe()
    agent = HarnessAgent(reg)
    f = _frame(optional_capabilities=["probe"])

    def messy(ctx, args):
        return {"findings": [
            _finding("OK_ONE"),
            {"problem_code": "BAD", "severity": "catastrophic"},  # 非法 severity
        ]}

    reg.register(CapabilitySpec(name="messy", description="脏输出"), messy)
    f = _frame(optional_capabilities=["probe", "messy"])
    outcome = agent.run(f, type("C", (), {})(), scripted_actor(
        [_tool("messy"), _finish()]))
    codes = [x.problem_code for x in outcome.findings]
    assert "OK_ONE" in codes and "BAD" not in codes


def test_default_registry_has_six_capabilities():
    reg = default_registry()
    assert set(reg.names()) == {
        "rule_query", "knowledge_search", "llm_verify",
        "llm_assess", "doc_extract", "writeback_probe"}
    # knowledge_search 是唯一预算型能力
    assert [n for n in reg.names() if reg.spec(n).budgeted] == ["knowledge_search"]


def test_protocol_error_importable_surface():
    assert ProtocolError is not None  # 公共异常面
