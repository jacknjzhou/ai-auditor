"""P3.0-T6 Decision Composer + 新示例 SOP（用章申请）—— "新流零代码接入"。

验收口径（设计 §9 T6）：仅写 `audit_sop` YAML + 路由表即接入新审批流，
不改动 Runner / Planner / Harness 任何代码。

1. SOP 配置文件装载 + 静态校验（坏配置装载期即失败）；
2. 零代码接入：install_flow_sop 写 SOP + 路由表 → 端到端 run 走新 SOP
   （帧档案节点序列 = YAML 节点序列，证明配置驱动执行）；
3. human_gate 挂起 + 恢复决策（贯通 T5）；
4. 运行时条件分支：on_hard_violation → __reject__ 终态（矩阵 REJECT 互证）；
5. Decision Composer：帧产出归一（findings/置信度/证据/复用标注）；
6. 管理 API：PUT/GET/validate。
"""
import uuid
from pathlib import Path

import pytest

from app.config import settings
from app.models.entities import (ApprovalEvent, AuditDecision, AuditTask, Base,
                                 FlowProfile, HumanTask, SessionLocal,
                                 TaskFrameRecord, engine, init_db)
from app.services.audit_service import run_audit_task
from app.services.kernel_runner import resume_kernel_run
from worker.pipeline.kernel.composer import DecisionComposer
from worker.pipeline.kernel.harness import FrameOutcome
from worker.pipeline.kernel.planner import AuditSOP
from worker.pipeline.kernel.sop_registry import (install_flow_sop, load_sop,
                                                 validate_sop)

SOP_DIR = Path(__file__).resolve().parents[1] / "config" / "sops"
SEAL_SOP = SOP_DIR / "seal_application.yaml"
SEAL_ROUTES = SOP_DIR / "seal_application.routes.json"

FLOW = "seal_application"


@pytest.fixture()
def db_session():
    Base.metadata.drop_all(engine)
    init_db()
    db = SessionLocal()
    yield db
    db.close()
    Base.metadata.drop_all(engine)


def _make_task(snapshot: dict, flow_code: str = FLOW) -> str:
    db = SessionLocal()
    ev = ApprovalEvent(source_code="oa-a8", instance_id="SEAL-1",
                       node_code="seal_review", event_type="submit",
                       idempotency_key=f"idem-{uuid.uuid4().hex}", raw_payload={})
    db.add(ev)
    db.flush()
    task = AuditTask(event_id=ev.id, source_code="oa-a8", instance_id="SEAL-1",
                     node_code="seal_review", flow_code=flow_code,
                     snapshot=snapshot)
    db.add(task)
    db.commit()
    tid = task.id
    db.close()
    return tid


def _frames(tid: str) -> list[TaskFrameRecord]:
    db = SessionLocal()
    rows = (db.query(TaskFrameRecord).filter_by(run_id=tid)
            .order_by(TaskFrameRecord.seq).all())
    db.close()
    return rows


# ---------------------------------------------------------------------------
# 1. SOP 配置文件装载 + 静态校验
# ---------------------------------------------------------------------------

def test_seal_sop_file_loads_and_validates():
    dsl = load_sop(SEAL_SOP)
    assert dsl["sop_code"] == "seal_application"
    assert dsl["entry"] == "intake"
    assert [n["id"] for n in dsl["nodes"]] == [
        "intake", "material", "policy", "gate", "risk", "writeback"]
    sop = AuditSOP(dsl)
    assert set(sop.nodes) == {"intake", "material", "policy", "gate",
                              "risk", "writeback"}


def test_invalid_sop_rejected_at_load_time():
    with pytest.raises(ValueError):
        validate_sop({"entry": "a", "nodes": [
            {"id": "a", "kind": "rule_eval",
             "transitions": [{"cond": "always", "next": "missing"}]}]})
    with pytest.raises(ValueError):
        validate_sop({"entry": "a", "nodes": [
            {"id": "a", "kind": "no_such_kind", "transitions": []}]})


# ---------------------------------------------------------------------------
# 2. 零代码接入：配置驱动执行（帧序列 = YAML 节点序列）
# ---------------------------------------------------------------------------

def test_zero_code_onboarding_runs_new_sop(db_session, monkeypatch):
    monkeypatch.setattr(settings, "kernel_mode", True)
    db = SessionLocal()
    install_flow_sop(db, FLOW, SEAL_SOP, route_table_path=SEAL_ROUTES,
                     mode="advisory")
    db.commit()
    profile = db.get(FlowProfile, FLOW)
    assert profile.audit_sop["sop_code"] == "seal_application"
    assert profile.route_table[0]["action"] == "ESCALATE"
    db.close()

    tid = _make_task({"form": {"amount": 2380, "trip_reason": "合同用章"}})
    run_audit_task(tid)  # 无任何代码改动，新流即按 YAML 执行

    yaml_nodes = [n["id"] for n in load_sop(SEAL_SOP)["nodes"]]
    executed = [f.node_id for f in _frames(tid)]
    # 帧节点序列是 YAML 节点序列的前缀（gate 处挂起，下游尚未执行）
    assert executed == yaml_nodes[:len(executed)]
    assert executed == ["intake", "material", "policy", "gate"]
    assert _status(tid) == "awaiting_human"


def _status(tid: str) -> str:
    db = SessionLocal()
    t = db.get(AuditTask, tid)
    db.close()
    return t.status


# ---------------------------------------------------------------------------
# 3. 挂起-恢复贯通：人工终审直写决策（T5 × T6）
# ---------------------------------------------------------------------------

def test_zero_code_flow_suspend_then_resume_decides(db_session, monkeypatch):
    monkeypatch.setattr(settings, "kernel_mode", True)
    db = SessionLocal()
    install_flow_sop(db, FLOW, SEAL_SOP, route_table_path=SEAL_ROUTES, mode="advisory")
    db.commit()
    db.close()

    tid = _make_task({"form": {"amount": 2380, "trip_reason": "合同用章"}})
    run_audit_task(tid)
    assert _status(tid) == "awaiting_human"

    db = SessionLocal()
    ht = db.query(HumanTask).filter_by(run_id=tid, status="pending").one()
    task = db.get(AuditTask, tid)
    out = resume_kernel_run(db, task, ht, action="OVERRIDE_PASS", operator="seal_mgr")
    db.commit()
    dec = db.query(AuditDecision).filter_by(task_id=tid).first()
    db.close()

    assert out["level"] == "PASS"
    assert dec.level == "PASS"
    assert dec.matrix_reason["cell"] == "human_override"


# ---------------------------------------------------------------------------
# 4. 运行时条件分支：on_hard_violation → __reject__（矩阵 REJECT 互证）
# ---------------------------------------------------------------------------

def test_zero_code_flow_hard_violation_rejects(db_session, monkeypatch):
    monkeypatch.setattr(settings, "kernel_mode", True)
    db = SessionLocal()
    install_flow_sop(db, FLOW, SEAL_SOP, mode="advisory")
    db.commit()
    db.close()

    # 大额触发 HIGH_AMOUNT（major）→ material 帧的 on_hard_violation 走 __reject__
    tid = _make_task({"form": {"amount": 500000, "trip_reason": "合同用章"}})
    run_audit_task(tid)

    assert _status(tid) == "decided"
    db = SessionLocal()
    dec = db.query(AuditDecision).filter_by(task_id=tid).first()
    db.close()
    assert dec.level == "REJECT"
    assert dec.matrix_reason["cell"] == "rule_hard_violation"
    # 终态 __reject__：material 后不再展开 policy/gate
    assert [f.node_id for f in _frames(tid)] == ["intake", "material"]


# ---------------------------------------------------------------------------
# 5. Decision Composer：帧产出归一
# ---------------------------------------------------------------------------

def test_decision_composer_normalizes_outcomes():
    from app.schemas.canonical import FindingOut

    oc1 = FrameOutcome(frame_id="r:f01", status="completed")
    oc1.findings = [FindingOut(problem_code="A", severity="minor",
                               title="a", detail="", engine="rule")]
    oc1.risk_flags = ["x"]
    oc2 = FrameOutcome(frame_id="r:f02", status="completed", confidence=0.88,
                       summary="总评", degraded=True)
    oc2.findings = [FindingOut(problem_code="B", severity="major",
                               title="b", detail="", engine="llm")]
    oc2.risk_flags = ["y"]  # Harness._apply_finish 已从 structured_result 归一

    composed = DecisionComposer().compose([oc1, oc2], terminal="__done__",
                                          executed_frames=["r:f01", "r:f02"],
                                          reused_frames=["r:f01"])
    assert [f.problem_code for f in composed.findings] == ["A", "B"]
    assert composed.confidence == 0.88
    assert composed.summary == "总评"
    assert composed.risk_flags == ["x", "y"]
    assert composed.degraded is True
    assert composed.terminal == "__done__"
    ev = composed.evidence
    assert ev["frames"] == 2 and ev["reused_frames"] == ["r:f01"]


# ---------------------------------------------------------------------------
# 6. 管理 API：PUT（安装）/ GET（读取）/ validate（预检）
# ---------------------------------------------------------------------------

def test_sop_api_install_get_validate(client, monkeypatch):
    monkeypatch.setattr(settings, "kernel_mode", True)
    dsl = load_sop(SEAL_SOP)
    routes = __import__("json").loads(SEAL_ROUTES.read_text(encoding="utf-8"))

    # validate：合法 → ok；非法 → 422
    r = client.post(f"/api/v1/flows/{FLOW}/sop/validate",
                    json={"sop": dsl})
    assert r.status_code == 200 and r.json()["n_nodes"] == 6
    r = client.post(f"/api/v1/flows/{FLOW}/sop/validate",
                    json={"sop": {"entry": "x", "nodes": []}})
    assert r.status_code == 422

    # PUT 安装
    r = client.put(f"/api/v1/flows/{FLOW}/sop",
                   json={"sop": dsl, "route_table": routes,
                         "mode": "advisory", "writeback_tier": "COMMENT_ONLY"})
    assert r.status_code == 200 and r.json()["installer"] == "zero-code"

    # GET 读取
    r = client.get(f"/api/v1/flows/{FLOW}/sop")
    assert r.status_code == 200
    body = r.json()
    assert body["is_builtin"] is False
    assert body["audit_sop"]["sop_code"] == "seal_application"
    assert len(body["route_table"]) == 3

    # 未知 flow → 404
    assert client.get("/api/v1/flows/unknown/sop").status_code == 404
