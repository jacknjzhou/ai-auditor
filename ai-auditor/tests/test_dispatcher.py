"""派发器与 LLM 链服务级集成测试。"""
from __future__ import annotations

import pytest

from app.config import settings
from app.models.entities import (AuditDecision, AuditFinding, ApprovalEvent,
                                 AuditTask, LlmCall, SessionLocal, init_db)
from app.services import dispatcher


class ThrowingChain:
    def run(self, snapshot, rule_findings, policy_excerpts=None):
        raise RuntimeError("boom")


class ConfidentChain:
    def __init__(self, conf=0.95):
        self.conf = conf

    def run(self, snapshot, rule_findings, policy_excerpts=None):
        from worker.pipeline.llm.chain import LLMChainResult, StepResult
        from worker.pipeline.llm.client import CallTrace
        trace = CallTrace(step="S4", model="stub", ok=True, latency_ms=5)
        return LLMChainResult(confidence=self.conf,
                              steps=[StepResult(step="S4", trace=trace)])


def _make_task(db, flow_code: str = "expense",
               form: dict | None = None) -> str:
    ev = ApprovalEvent(source_code="oa-a8", instance_id=f"I-{form}",
                       node_code="fin_review", event_type="NODE_ARRIVED",
                       idempotency_key=f"key-{flow_code}-{form}", version_no=1,
                       raw_payload={})
    db.add(ev)
    db.flush()
    task = AuditTask(event_id=ev.id, source_code="oa-a8",
                     instance_id=f"I-{form}", node_code="fin_review",
                     flow_code=flow_code,
                     snapshot={"form": form or {"amount": 500, "trip_reason": "出差"},
                               "attachments": []},
                     status="received", mode="shadow")
    db.add(task)
    db.commit()
    return task.id


@pytest.fixture()
def db():
    init_db()
    s = SessionLocal()
    yield s
    s.close()


def test_service_uses_llm_confidence(db):
    """stub 链高置信 + 规则无发现 → 决策记录 LLM 置信度，llm_call 留痕落库。"""
    from app.services.audit_service import run_audit_task
    task_id = _make_task(db, form={"amount": 500, "trip_reason": "出差"})
    run_audit_task(task_id, llm_chain=ConfidentChain(0.95))
    t = db.get(AuditTask, task_id)
    assert t.status == "decided"
    dec = db.query(AuditDecision).filter_by(task_id=task_id).first()
    assert dec.confidence == 0.95
    assert dec.level == "ADVISORY"  # auto_allowed=False → 高置信也只 ADVISORY
    calls = db.query(LlmCall).filter_by(task_id=task_id).all()
    assert len(calls) == 1 and calls[0].step == "S4"


def test_service_survives_chain_crash(db):
    """链抛异常 → 降级为无 LLM 信号，规则 findings 不丢失（不回滚）。"""
    from app.services.audit_service import run_audit_task
    task_id = _make_task(db, form={"amount": 500, "trip_reason": ""})  # 触发缺事由规则
    run_audit_task(task_id, llm_chain=ThrowingChain())
    t = db.get(AuditTask, task_id)
    assert t.status == "decided"
    dec = db.query(AuditDecision).filter_by(task_id=task_id).first()
    assert dec.confidence is None
    f = db.query(AuditFinding).filter_by(task_id=task_id,
                                         problem_code="MISSING_FIELD").first()
    assert f is not None


def test_hard_violation_beats_llm_confidence(db):
    """规则 major 硬违规 → REJECT，与 LLM 置信度无关（矩阵 gate）。"""
    from app.services.audit_service import run_audit_task
    task_id = _make_task(db, form={"amount": 500000, "trip_reason": "出差"})
    run_audit_task(task_id, llm_chain=ConfidentChain(0.99))
    dec = db.query(AuditDecision).filter_by(task_id=task_id).first()
    assert dec.level == "REJECT"


@pytest.mark.anyio
async def test_dispatch_arq_success_path(monkeypatch):
    """arq 模式 + 队列可达 → 返回 arq，不执行 inline。"""
    executed = []

    async def fake_enqueue(task_id):
        return True

    monkeypatch.setattr(settings, "queue_mode", "arq")
    monkeypatch.setattr(dispatcher, "enqueue_arq", fake_enqueue)
    monkeypatch.setattr(dispatcher, "run_audit_task_inline",
                        lambda tid: executed.append(tid))
    channel = await dispatcher.dispatch_audit_task("t-1", background=None)
    assert channel == "arq"
    assert executed == []


@pytest.mark.anyio
async def test_dispatch_arq_fallback_to_inline(monkeypatch):
    """arq 模式 + Redis 不可达 → 降级 inline 执行。"""
    executed = []

    async def fake_enqueue(task_id):
        return False  # 真实 enqueue_arq 内部捕获异常后返回 False

    monkeypatch.setattr(settings, "queue_mode", "arq")
    monkeypatch.setattr(dispatcher, "enqueue_arq", fake_enqueue)
    monkeypatch.setattr(dispatcher, "run_audit_task_inline",
                        lambda tid: executed.append(tid))
    channel = await dispatcher.dispatch_audit_task("t-2", background=None)
    assert channel == "inline"
    assert executed == ["t-2"]


@pytest.mark.anyio
async def test_dispatch_inline_default(monkeypatch):
    """默认 inline 模式：background=None 时同步执行。"""
    executed = []
    monkeypatch.setattr(settings, "queue_mode", "inline")
    monkeypatch.setattr(dispatcher, "run_audit_task_inline",
                        lambda tid: executed.append(tid))
    channel = await dispatcher.dispatch_audit_task("t-3", background=None)
    assert channel == "inline"
    assert executed == ["t-3"]


@pytest.fixture()
def anyio_backend():
    return "asyncio"
