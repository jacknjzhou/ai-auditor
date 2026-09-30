"""P3.0-T4 run_event 全链路埋点 + capability_call 预算审计（v3.0 设计 §6/§7）。

验收口径（设计 §9 T4）：一次完整 run 的事件序列快照比对；
1. 事件序列快照：内置 SOP 干净单据 run 的 (seq, event_type, frame_id) 全序列；
2. capability_call 留痕：每帧一次能力调用、digest 前缀、知识预算余量；
3. 增量查询 API：after_seq 游标过滤 + 404；
4. protocol_repair / awaiting_human 事件；
5. composed / writeback_applied 与 decision/escalation 落库互证。
"""
import uuid

import pytest

from app.config import settings
from app.models.entities import (ApprovalEvent, AuditDecision, AuditTask,
                                 Base, CapabilityCallRecord, EscalationLog,
                                 FlowProfile, RunEvent, SessionLocal, engine,
                                 init_db)
from app.services.audit_service import run_audit_task
from worker.pipeline.kernel.capabilities import (CapabilityContext,
                                                 default_registry)
from worker.pipeline.kernel.events import EventRecorder
from worker.pipeline.kernel.frames import make_frame
from worker.pipeline.kernel.harness import HarnessAgent, scripted_actor

SNAP_CLEAN = {"form": {"amount": 2380, "trip_reason": "北京出差"}}
SNAP_BIG = {"form": {"amount": 500000, "trip_reason": "北京出差"}}


@pytest.fixture()
def db_session():
    Base.metadata.drop_all(engine)
    init_db()
    db = SessionLocal()
    yield db
    db.close()
    Base.metadata.drop_all(engine)


def _make_task(snapshot: dict, flow_code: str = "expense",
               node: str = "fin_review") -> str:
    db = SessionLocal()
    ev = ApprovalEvent(source_code="oa-a8", instance_id="I-1", node_code=node,
                       event_type="submit",
                       idempotency_key=f"idem-{uuid.uuid4().hex}", raw_payload={})
    db.add(ev)
    db.flush()
    task = AuditTask(event_id=ev.id, source_code="oa-a8", instance_id="I-1",
                     node_code=node, flow_code=flow_code, snapshot=snapshot)
    db.add(task)
    db.commit()
    tid = task.id
    db.close()
    return tid


def _events(tid: str) -> list[RunEvent]:
    db = SessionLocal()
    rows = (db.query(RunEvent).filter_by(run_id=tid)
            .order_by(RunEvent.seq).all())
    db.close()
    return rows


# ---------------------------------------------------------------------------
# 1. 事件序列快照：内置 SOP 无 LLM 干净单据（5 帧，运行时条件两轮二次展开）
# ---------------------------------------------------------------------------

def test_event_sequence_snapshot(db_session, monkeypatch):
    monkeypatch.setattr(settings, "kernel_mode", True)
    tid = _make_task(snapshot=SNAP_CLEAN)
    run_audit_task(tid)

    rows = _events(tid)
    # 内置 SOP：intake→material 首轮展开；policy→risk 二轮；writeback 三轮
    f1, f2, f3, f4, f5 = (f"{tid}:f{i:02d}" for i in range(1, 6))
    expected = [
        (1, "frame_planned", f1), (2, "frame_planned", f2),
        (3, "frame_started", f1),
        (4, "capability_called", f1), (5, "capability_result", f1),
        (6, "frame_finished", f1),
        (7, "frame_started", f2),
        (8, "capability_called", f2), (9, "capability_result", f2),
        (10, "frame_finished", f2),
        (11, "frame_planned", f3), (12, "frame_planned", f4),
        (13, "frame_started", f3),
        (14, "capability_called", f3), (15, "capability_result", f3),
        (16, "frame_finished", f3),
        (17, "frame_started", f4),
        (18, "capability_called", f4), (19, "capability_result", f4),
        (20, "frame_finished", f4),
        (21, "frame_planned", f5),
        (22, "frame_started", f5),
        (23, "capability_called", f5), (24, "capability_result", f5),
        (25, "frame_finished", f5),
        (26, "composed", ""), (27, "writeback_applied", ""),
    ]
    actual = [(r.seq, r.event_type, r.frame_id) for r in rows]
    assert actual == expected
    # seq 严格单调连续，无空洞
    assert [r.seq for r in rows] == list(range(1, len(rows) + 1))


# ---------------------------------------------------------------------------
# 2. capability_call 留痕：能力序列 / digest / 知识预算余量
# ---------------------------------------------------------------------------

def test_capability_call_rows(db_session, monkeypatch):
    monkeypatch.setattr(settings, "kernel_mode", True)
    tid = _make_task(snapshot=SNAP_CLEAN)
    run_audit_task(tid)

    db = SessionLocal()
    rows = (db.query(CapabilityCallRecord)
            .filter_by(run_id=tid).order_by(CapabilityCallRecord.id).all())
    db.close()
    assert [r.capability for r in rows] == [
        "rule_query", "llm_verify", "knowledge_search", "llm_assess",
        "writeback_probe"]
    assert all(r.ok for r in rows)
    assert all(r.arguments_digest.startswith("sha256:") for r in rows)
    assert all(r.result_digest.startswith("sha256:") for r in rows)
    # policy_check 节点 knowledge_budget=3，一次成功检索后余量 2
    ks = rows[2]
    assert ks.budget_left == 2
    assert rows[0].budget_left is None  # 非预算能力不记账


# ---------------------------------------------------------------------------
# 3. 增量查询 API：after_seq 游标 + 404
# ---------------------------------------------------------------------------

def test_incremental_events_api(db_session, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setattr(settings, "kernel_mode", True)
    tid = _make_task(snapshot=SNAP_CLEAN)
    run_audit_task(tid)

    with TestClient(app) as c:
        r = c.get(f"/api/v1/runs/{tid}/events")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] >= 25 and body["events"][0]["seq"] == 1

        # 游标增量：after_seq=20 只返回其后的事件
        r2 = c.get(f"/api/v1/runs/{tid}/events?after_seq=20")
        seqs = [e["seq"] for e in r2.json()["events"]]
        assert seqs == sorted(seqs) and all(s > 20 for s in seqs)
        assert r2.json()["events"][0]["event_type"] == "frame_planned"

        # 能力调用留痕 API
        r3 = c.get(f"/api/v1/runs/{tid}/capability-calls")
        assert r3.status_code == 200
        assert [x["capability"] for x in r3.json()["calls"]] == [
            "rule_query", "llm_verify", "knowledge_search", "llm_assess",
            "writeback_probe"]

        # 未知 run → 404
        assert c.get("/api/v1/runs/nope/events").status_code == 404
        assert c.get("/api/v1/runs/nope/capability-calls").status_code == 404


# ---------------------------------------------------------------------------
# 4. protocol_repair 事件：非法 actor 输出进入修复链并留痕
# ---------------------------------------------------------------------------

def test_protocol_repair_event():
    rec = EventRecorder()
    harness = HarnessAgent(default_registry(), on_event=rec.emit)
    frame = make_frame("run-x", 1, "rule_eval",
                       goal="执行全部启用规则",
                       node_id="intake",
                       required_capabilities=["rule_query"])
    outcome = harness.run(frame, CapabilityContext(),
                          scripted_actor(["不是 JSON", "也不是 JSON", "仍不是 JSON"]))

    assert outcome.status == "failed"
    repairs = [(e.seq, e.payload.get("reason")) for e in rec._events
               if e.event_type == "protocol_repair"]
    assert len(repairs) == 2  # repair_attempts=2，第三次超限置 failed
    assert all(reason for _, reason in repairs)
    # 事件序：repair → repair →（无 finish，loop 正常退出）
    assert rec.snapshot()[-1][1] == "protocol_repair"


# ---------------------------------------------------------------------------
# 5. awaiting_human 事件：human_gate 帧挂起留痕
# ---------------------------------------------------------------------------

GATE_SOP = {
    "sop_code": "gate",
    "entry": "intake",
    "nodes": [
        {"id": "intake", "kind": "rule_eval",
         "required_capabilities": ["rule_query"],
         "transitions": [{"cond": "always", "next": "gate"}]},
        {"id": "gate", "kind": "human_gate",
         "transitions": [{"cond": "always", "next": "__done__"}]},
    ],
}


def test_awaiting_human_event(db_session, monkeypatch):
    monkeypatch.setattr(settings, "kernel_mode", True)
    tid = _make_task(snapshot=SNAP_BIG)
    db = SessionLocal()
    db.add(FlowProfile(flow_code="expense", mode="advisory", audit_sop=GATE_SOP))
    db.commit()
    db.close()

    run_audit_task(tid)
    rows = _events(tid)
    ah = [r for r in rows if r.event_type == "awaiting_human"]
    assert len(ah) == 1
    assert ah[0].frame_id == f"{tid}:f02"  # gate 帧
    # awaiting_human 后无后续帧（run 停止）：frame_finished → composed → writeback_applied
    tail = [(r.seq, r.event_type) for r in rows if r.seq > ah[0].seq]
    assert tail == [(ah[0].seq + 1, "frame_finished"),
                    (ah[0].seq + 2, "composed"),
                    (ah[0].seq + 3, "writeback_applied")]


# ---------------------------------------------------------------------------
# 6. composed / writeback_applied：与 decision / escalation 落库互证
# ---------------------------------------------------------------------------

def test_composed_and_writeback_payloads(db_session, monkeypatch):
    monkeypatch.setattr(settings, "kernel_mode", True)
    tid = _make_task(snapshot=SNAP_CLEAN)
    run_audit_task(tid)

    db = SessionLocal()
    decision = db.query(AuditDecision).filter_by(task_id=tid).first()
    esc = db.query(EscalationLog).filter_by(task_id=tid).first()
    db.close()

    rows = _events(tid)
    composed = next(r for r in rows if r.event_type == "composed")
    wb = next(r for r in rows if r.event_type == "writeback_applied")
    assert composed.payload["level"] == decision.level
    assert composed.payload["confidence"] is None  # 无 LLM
    assert wb.payload["level"] == decision.level
    assert wb.payload["writeback_status"] == (esc.writeback_status if esc else "none")
    # shadow 模式：决策产出但不回写（无 LLM 干净单 → ADVISORY）
    assert decision.level == "ADVISORY"
    assert wb.payload["writeback_status"] == "skipped_shadow"


# ---------------------------------------------------------------------------
# 7. EventRecorder 契约：未知事件类型拒绝 / flush 幂等
# ---------------------------------------------------------------------------

def test_recorder_contract():
    rec = EventRecorder()
    with pytest.raises(ValueError):
        rec.emit("bogus_event")
    s1 = rec.snapshot()
    rec.emit("frame_planned", frame_id="f1", kind="rule_eval")
    assert len(rec.snapshot()) == len(s1) + 1

    class _FakeDB:
        def __init__(self):
            self.added = []

        def add(self, obj):
            self.added.append(obj)

    fake = _FakeDB()
    assert rec.flush(fake, "run-1") == 1
    assert rec.flush(fake, "run-1") == 0  # 幂等
    assert len(fake.added) == 1
