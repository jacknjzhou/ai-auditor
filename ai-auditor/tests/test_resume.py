"""P3.0-T5 挂起-恢复 + 增量重审（v3.0 设计 §6 / v2.1 §1）。

验收口径（设计 §9 T5）：挂起 → 恢复 → 只重跑受影响帧 的端到端用例。

1. human_gate 挂起：写 human_task（resume_token）、task.status=awaiting_human、不出决策；
2. COMMENT 恢复：已完成帧全复用（reused），从 gate 续跑下游 → 决策落库；
   链结果随 run_cache 还原，下游 LLM 帧零重复调用；
3. SUPPLY_MATERIAL：patch 合并 + 规则全量重算 + 指纹复用（摘录未变的 policy 帧
   reused，材料类帧重跑）+ 再次挂起；
4. OVERRIDE_*：不经流水线直写终审（matrix_reason.cell=human_override）；
5. 幂等：重复 resume 不产生二次事件/决策（human_task.status=done 短路）；
6. max_suspend_rounds：超限强制出决策（防 human_gate 死循环）；
7. 事件续写：resumed 事件 + (run_id, seq) 无冲突、严格递增；
8. API 层：POST /audit-tasks/{id}/resume（HMAC 验签 / 422 / 404）。
"""
import uuid

import pytest

from app.config import settings
from app.models.entities import (ApprovalEvent, AuditDecision, AuditFinding,
                                 AuditTask, Base, EscalationLog, FlowProfile,
                                 HumanTask, RunEvent, SessionLocal,
                                 TaskFrameRecord, engine, init_db)
from app.schemas.canonical import FindingOut
from app.services.audit_service import run_audit_task
from app.services.kernel_runner import resume_kernel_run

SNAP_OK = {"form": {"amount": 2380, "trip_reason": "北京出差"}}

# 六节点 SOP：rule_eval → llm_verify → knowledge_check → human_gate
#            → llm_assess → writeback；gate 为唯一挂起点。
T5_SOP = {
    "sop_code": "t5_gate",
    "entry": "intake",
    "nodes": [
        {"id": "intake", "kind": "rule_eval",
         "required_capabilities": ["rule_query"],
         "transitions": [{"cond": "always", "next": "material"}]},
        {"id": "material", "kind": "llm_verify",
         "required_capabilities": ["llm_verify"],
         "failure_policy": "degrade",
         "transitions": [{"cond": "always", "next": "policy"}]},
        {"id": "policy", "kind": "knowledge_check",
         "required_capabilities": ["knowledge_search"],
         "knowledge_budget": 3,
         "transitions": [{"cond": "always", "next": "gate"}]},
        {"id": "gate", "kind": "human_gate",
         "instruction": "低置信度或重大违规时挂起等待实体人",
         "transitions": [{"cond": "always", "next": "risk"}]},
        {"id": "risk", "kind": "risk_assess",
         "required_capabilities": ["llm_assess"],
         "transitions": [{"cond": "always", "next": "writeback"}]},
        {"id": "writeback", "kind": "writeback",
         "required_capabilities": ["writeback_probe"],
         "transitions": [{"cond": "always", "next": "__done__"}]},
    ],
}


@pytest.fixture()
def db_session():
    Base.metadata.drop_all(engine)
    init_db()
    db = SessionLocal()
    yield db
    db.close()
    Base.metadata.drop_all(engine)


class _StubChain:
    """LLM 链 stub：记录调用次数（验证恢复后零重复调用）。"""

    def __init__(self, conf: float = 0.93):
        self.conf = conf
        self.calls = 0

    def run(self, snapshot, rule_findings, policy_excerpts=None):
        from worker.pipeline.llm.chain import LLMChainResult, StepResult
        from worker.pipeline.llm.client import CallTrace

        self.calls += 1
        steps = []
        for code, problem in (("S1", "MATERIAL_MISSING"), ("S2", "INCONSISTENT"),
                              ("S3", "POLICY_RISK"), ("S4", "RISK_FLAG")):
            f = FindingOut(problem_code=problem, severity="minor",
                           title=f"{code} 问题", detail="stub", engine="llm")
            tr = CallTrace(step=code, model="stub", ok=True, latency_ms=1, attempts=1)
            steps.append(StepResult(step=code, findings=[f], trace=tr))
        result = LLMChainResult(confidence=self.conf, summary="stub 评估", steps=steps)
        result.findings = [f for s in steps for f in s.findings]
        return result


def _make_task(snapshot: dict, sop: dict | None = T5_SOP) -> str:
    db = SessionLocal()
    ev = ApprovalEvent(source_code="oa-a8", instance_id="I-1", node_code="fin_review",
                       event_type="submit",
                       idempotency_key=f"idem-{uuid.uuid4().hex}", raw_payload={})
    db.add(ev)
    db.flush()
    task = AuditTask(event_id=ev.id, source_code="oa-a8", instance_id="I-1",
                     node_code="fin_review", flow_code="expense", snapshot=snapshot)
    db.add(task)
    if sop is not None:
        db.add(FlowProfile(flow_code="expense", mode="advisory", audit_sop=sop))
    db.commit()
    tid = task.id
    db.close()
    return tid


def _task(tid: str) -> AuditTask:
    db = SessionLocal()
    t = db.get(AuditTask, tid)
    db.close()
    return t


def _events(tid: str) -> list[RunEvent]:
    db = SessionLocal()
    rows = db.query(RunEvent).filter_by(run_id=tid).order_by(RunEvent.seq).all()
    db.close()
    return rows


def _frames(tid: str) -> list[TaskFrameRecord]:
    db = SessionLocal()
    rows = (db.query(TaskFrameRecord).filter_by(run_id=tid)
            .order_by(TaskFrameRecord.seq).all())
    db.close()
    return rows


def _decision(tid: str) -> AuditDecision | None:
    db = SessionLocal()
    d = db.query(AuditDecision).filter_by(task_id=tid).first()
    db.close()
    return d


def _pending_ht(tid: str) -> HumanTask | None:
    db = SessionLocal()
    ht = (db.query(HumanTask).filter_by(run_id=tid, status="pending")
          .order_by(HumanTask.created_at.desc()).first())
    db.close()
    return ht


def _resume(tid: str, action: str, *, patch=None, operator: str = "u1",
            llm_chain=None) -> dict:
    db = SessionLocal()
    task = db.get(AuditTask, tid)
    ht = (db.query(HumanTask).filter_by(run_id=tid)
          .order_by(HumanTask.created_at.desc(), HumanTask.id.desc()).first())
    payload = {"patch_fields": patch} if patch else {}
    out = resume_kernel_run(db, task, ht, action=action, operator=operator,
                            payload=payload, llm_chain=llm_chain)
    db.commit()
    db.close()
    return out


# ---------------------------------------------------------------------------
# 1. COMMENT 恢复：全帧复用 + 从 gate 续跑下游 + 出决策（链零重复调用）
# ---------------------------------------------------------------------------

def test_comment_resume_reuses_all_frames_and_decides(db_session, monkeypatch):
    monkeypatch.setattr(settings, "kernel_mode", True)
    tid = _make_task(SNAP_OK)
    chain = _StubChain()

    run_audit_task(tid, llm_chain=chain)
    assert chain.calls == 1
    assert _task(tid).status == "awaiting_human"
    assert _decision(tid) is None  # 挂起期不出决策

    out = _resume(tid, "COMMENT", llm_chain=chain)
    assert out["resumed"] is True
    assert out["level"] is not None
    assert chain.calls == 1, "链结果已还原，下游 LLM 帧零重复调用"
    assert _task(tid).status == "decided"

    # 挂起前完成的 intake/material/policy 三帧全部复用
    reused = {f.frame_id for f in _frames(tid) if (f.outcome or {}).get("reused")}
    assert reused == {f"{tid}:f01", f"{tid}:f02", f"{tid}:f03"}

    rows = _events(tid)
    assert any(r.event_type == "resumed" and r.payload["action"] == "COMMENT"
               for r in rows)
    assert any(r.event_type == "frame_finished" and r.payload.get("reused")
               for r in rows)
    # seq 严格递增、无重复（resume 续写不与首轮冲突）
    seqs = [r.seq for r in rows]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))


# ---------------------------------------------------------------------------
# 2. SUPPLY_MATERIAL：规则全量重算 + 指纹复用（policy 复用 / 材料类重跑）
# ---------------------------------------------------------------------------

def test_supply_material_reuses_unaffected_frames(db_session, monkeypatch):
    monkeypatch.setattr(settings, "kernel_mode", True)
    tid = _make_task(SNAP_OK)
    chain = _StubChain()
    run_audit_task(tid, llm_chain=chain)
    assert _task(tid).status == "awaiting_human"

    # patch amount：规则 scope 变 → intake/material 指纹变（重跑）；
    # trip_reason 未变 → rag query 不变 → policy 帧指纹不变（复用）
    out = _resume(tid, "SUPPLY_MATERIAL", patch={"amount": 3000}, llm_chain=chain)
    assert out["status"] == "awaiting_human"  # gate 再次挂起（人审未决议下游）
    assert _task(tid).snapshot["form"]["amount"] == 3000  # patch 已合并

    # policy 原帧复用；材料类帧以新 seq 重跑
    reused = {f.frame_id for f in _frames(tid) if (f.outcome or {}).get("reused")}
    assert reused == {f"{tid}:f03"}  # policy 摘录未变 → reused

    rows = _events(tid)
    assert any(r.event_type == "resumed" and r.payload["action"] == "SUPPLY_MATERIAL"
               for r in rows)
    called = {r.frame_id for r in rows if r.event_type == "capability_called"}
    assert f"{tid}:f05" in called  # 二轮 intake 重跑（seq 偏移）
    assert f"{tid}:f06" in called  # 二轮 material 重跑
    assert f"{tid}:f07" not in called  # policy 复用 → 未重复执行


# ---------------------------------------------------------------------------
# 3. OVERRIDE：不经流水线直写终审（矩阵之上的权威，全程留痕）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action,level", [("OVERRIDE_PASS", "PASS"),
                                          ("OVERRIDE_REJECT", "REJECT")])
def test_override_writes_decision_directly(db_session, monkeypatch, action, level):
    monkeypatch.setattr(settings, "kernel_mode", True)
    tid = _make_task(SNAP_OK)
    run_audit_task(tid)  # 无 LLM：仍挂起于 gate
    assert _task(tid).status == "awaiting_human"

    out = _resume(tid, action, operator="u9")
    assert out["level"] == level
    assert _task(tid).status == "decided"

    dec = _decision(tid)
    assert dec.level == level
    assert dec.matrix_reason["cell"] == "human_override"
    assert dec.matrix_reason["overridden"] is True
    assert dec.matrix_reason["operator"] == "u9"
    # 人工决议留痕 + 幂等标记
    db = SessionLocal()
    ht = db.query(HumanTask).filter_by(run_id=tid).one()
    assert ht.status == "done" and ht.result["action"] == action
    db.close()


# ---------------------------------------------------------------------------
# 4. 幂等：重复 resume 短路（不产生二次事件/决策）
# ---------------------------------------------------------------------------

def test_resume_is_idempotent(db_session, monkeypatch):
    monkeypatch.setattr(settings, "kernel_mode", True)
    tid = _make_task(SNAP_OK)
    run_audit_task(tid)

    first = _resume(tid, "OVERRIDE_PASS")
    second = _resume(tid, "OVERRIDE_PASS")
    assert first["resumed"] is True and first["level"] == "PASS"
    assert second["resumed"] is False and second["deduplicated"] is True

    db = SessionLocal()
    assert db.query(AuditDecision).filter_by(task_id=tid).count() == 1
    assert db.query(RunEvent).filter_by(run_id=tid).filter(
        RunEvent.event_type == "resumed").count() == 1  # 二次 resume 未续写事件
    db.close()


# ---------------------------------------------------------------------------
# 5. max_suspend_rounds：超限强制出决策（防 human_gate 死循环）
# ---------------------------------------------------------------------------

def test_max_suspend_rounds_forces_decision(db_session, monkeypatch):
    monkeypatch.setattr(settings, "kernel_mode", True)
    monkeypatch.setattr(settings, "max_suspend_rounds", 1)
    tid = _make_task(SNAP_OK)
    run_audit_task(tid)
    assert _task(tid).status == "awaiting_human"

    # SUPPLY 会从 entry 重跑并在 gate 再次触发挂起，但轮次已达上限 → 强制出决策
    out = _resume(tid, "SUPPLY_MATERIAL", patch={"amount": 3000})
    assert out.get("status") != "awaiting_human"
    assert out["level"] is not None
    assert _task(tid).status == "decided"
    assert _decision(tid).level == out["level"]
    # 未产生第二轮 human_task（挂起被上限拒绝）
    db = SessionLocal()
    assert db.query(HumanTask).filter_by(run_id=tid).count() == 1
    db.close()


# ---------------------------------------------------------------------------
# 6. 帧档案：挂起时完成的帧已落库（增量重审复用基础）
# ---------------------------------------------------------------------------

def test_frame_archive_persisted_on_suspend(db_session, monkeypatch):
    monkeypatch.setattr(settings, "kernel_mode", True)
    tid = _make_task(SNAP_OK)
    run_audit_task(tid)

    frames = _frames(tid)
    # 首轮完成帧：intake/material/policy/gate（gate 状态 awaiting_human）
    assert [f.node_id for f in frames] == ["intake", "material", "policy", "gate"]
    assert {f.status for f in frames} == {"completed", "awaiting_human"}
    for f in frames:
        assert f.outcome and f.outcome.get("fingerprint")


# ---------------------------------------------------------------------------
# 7. API 层：POST /audit-tasks/{id}/resume（HMAC 验签 / 404 / 422）
# ---------------------------------------------------------------------------

def test_resume_api_requires_signature(client, sign, monkeypatch):
    monkeypatch.setattr(settings, "kernel_mode", True)
    tid = _make_task(SNAP_OK)
    run_audit_task(tid)
    ht = _pending_ht(tid)
    body = {"action": "OVERRIDE_PASS", "resume_token": ht.resume_token,
            "operator": "u1"}

    r = client.post(f"/api/v1/audit-tasks/{tid}/resume", json=body)
    assert r.status_code == 401  # 无签名


def test_resume_api_happy_and_token_mismatch(client, sign, monkeypatch):
    monkeypatch.setattr(settings, "kernel_mode", True)
    tid = _make_task(SNAP_OK)
    run_audit_task(tid)
    ht = _pending_ht(tid)

    # token 不匹配 → 404
    bad = {"action": "OVERRIDE_PASS", "resume_token": "deadbeef"}
    raw, headers = sign(bad)
    r = client.post(f"/api/v1/audit-tasks/{tid}/resume", content=raw, headers=headers)
    assert r.status_code == 404

    # action 非法 → 422
    bad2 = {"action": "NOPE", "resume_token": ht.resume_token}
    raw, headers = sign(bad2)
    r = client.post(f"/api/v1/audit-tasks/{tid}/resume", content=raw, headers=headers)
    assert r.status_code == 422

    # 正常恢复 → 200 + 决策级别
    good = {"action": "OVERRIDE_REJECT", "resume_token": ht.resume_token,
            "operator": "u1"}
    raw, headers = sign(good)
    r = client.post(f"/api/v1/audit-tasks/{tid}/resume", content=raw, headers=headers)
    assert r.status_code == 200
    assert r.json()["level"] == "REJECT"
