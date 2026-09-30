"""P3.0-T3 内核接管流水线 —— 等价迁移验收（v3.0 设计 §6）。

验收硬标准：
1. 既有 51+ 测试零回归（kernel_mode 默认 false，legacy 路径原样保留）；
2. 等价迁移 case 逐一对齐：同一单据快照 + 同一规则集 + 同一 LLM stub，
   legacy 与 kernel 两条路径产出相同的 findings / decision / llm_call 留痕。
"""
import uuid

import pytest

from app.config import settings
from app.models.entities import (ApprovalEvent, AuditDecision, AuditFinding,
                                 AuditTask, Base, FlowProfile, LlmCall,
                                 SessionLocal, engine, init_db)
from app.schemas.canonical import FindingOut
from app.services.audit_service import run_audit_task

SNAP_CLEAN = {"form": {"amount": 2380, "trip_reason": "北京出差"}}
SNAP_BIG = {"form": {"amount": 500000, "trip_reason": "北京出差"}}
SNAP_MINOR = {"form": {"amount": 2380, "trip_reason": ""}}


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


def _load(task_id: str) -> dict:
    db = SessionLocal()
    task = db.get(AuditTask, task_id)
    findings = db.query(AuditFinding).filter_by(task_id=task_id).all()
    decision = db.query(AuditDecision).filter_by(task_id=task_id).first()
    llm_rows = db.query(LlmCall).filter_by(task_id=task_id).count()
    out = {
        "status": task.status,
        "level": decision.level if decision else None,
        "confidence": decision.confidence if decision else None,
        "problems": sorted(f.problem_code for f in findings),
        "llm_rows": llm_rows,
    }
    db.close()
    return out


# ---------------------------------------------------------------------------
# LLM 链 stub（与 test_llm_chain 同风格：S1–S4 各一 finding + trace）
# ---------------------------------------------------------------------------

class _StubChain:
    def __init__(self, conf: float = 0.93):
        self.conf = conf
        self.seen_excerpts = None

    def run(self, snapshot, rule_findings, policy_excerpts=None):
        from worker.pipeline.llm.chain import LLMChainResult, StepResult
        from worker.pipeline.llm.client import CallTrace

        self.seen_excerpts = policy_excerpts
        steps = []
        for code, problem in (("S1", "MATERIAL_MISSING"), ("S2", "INCONSISTENT"),
                              ("S3", "POLICY_RISK"), ("S4", "RISK_FLAG")):
            f = FindingOut(problem_code=problem, severity="minor",
                           title=f"{code} 问题", detail="stub", engine="llm")
            tr = CallTrace(step=code, model="stub", ok=True,
                           latency_ms=1, attempts=1)
            steps.append(StepResult(step=code, findings=[f], trace=tr))
        result = LLMChainResult(confidence=self.conf, summary="stub 评估",
                                steps=steps)
        result.findings = [f for s in steps for f in s.findings]
        return result


class _BoomChain:
    def run(self, *args, **kwargs):
        raise RuntimeError("gateway exploded")


# ---------------------------------------------------------------------------
# 1. 无 LLM：等价迁移三 case（干净单 / 大额硬违规 / 缺字段）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("snapshot", [SNAP_CLEAN, SNAP_BIG, SNAP_MINOR],
                         ids=["clean", "big_amount", "missing_field"])
def test_kernel_equivalent_legacy_rules_only(db_session, monkeypatch, snapshot):
    legacy_id = _make_task(snapshot=snapshot)
    run_audit_task(legacy_id)
    legacy = _load(legacy_id)

    monkeypatch.setattr(settings, "kernel_mode", True)
    kernel_id = _make_task(snapshot=snapshot)
    run_audit_task(kernel_id)
    kernel = _load(kernel_id)

    assert kernel["status"] == "decided"
    assert kernel["level"] == legacy["level"]
    assert kernel["problems"] == legacy["problems"]
    assert kernel["confidence"] == legacy["confidence"]
    assert kernel["llm_rows"] == legacy["llm_rows"] == 0


# ---------------------------------------------------------------------------
# 2. 有 LLM stub：置信度 / findings / llm_call 留痕全对齐
# ---------------------------------------------------------------------------

def test_kernel_equivalent_with_llm_chain(db_session, monkeypatch):
    legacy_id = _make_task(snapshot=SNAP_CLEAN)
    run_audit_task(legacy_id, llm_chain=_StubChain())
    legacy = _load(legacy_id)

    monkeypatch.setattr(settings, "kernel_mode", True)
    kernel_id = _make_task(snapshot=SNAP_CLEAN)
    run_audit_task(kernel_id, llm_chain=_StubChain())
    kernel = _load(kernel_id)

    assert legacy["level"] == kernel["level"]
    assert legacy["confidence"] == kernel["confidence"] == 0.93
    assert kernel["problems"] == legacy["problems"]
    assert "MATERIAL_MISSING" in kernel["problems"]  # LLM finding 经帧协议进入
    assert kernel["llm_rows"] == legacy["llm_rows"] == 4


# ---------------------------------------------------------------------------
# 3. 链崩溃降级：两条路径都退化为 rules-only，决策不变
# ---------------------------------------------------------------------------

def test_kernel_degrades_on_chain_failure(db_session, monkeypatch):
    legacy_id = _make_task(snapshot=SNAP_CLEAN)
    run_audit_task(legacy_id, llm_chain=_BoomChain())
    legacy = _load(legacy_id)

    monkeypatch.setattr(settings, "kernel_mode", True)
    kernel_id = _make_task(snapshot=SNAP_CLEAN)
    run_audit_task(kernel_id, llm_chain=_BoomChain())
    kernel = _load(kernel_id)

    assert kernel["status"] == "decided"
    assert kernel["level"] == legacy["level"] == "ADVISORY"
    assert kernel["confidence"] is None and legacy["confidence"] is None
    assert kernel["problems"] == legacy["problems"]
    assert kernel["llm_rows"] == legacy["llm_rows"] == 0


# ---------------------------------------------------------------------------
# 4. 内置 SOP 运行时条件：大额单据走 __reject__ 终态（与矩阵 REJECT 互证）
# ---------------------------------------------------------------------------

def test_builtin_sop_hard_violation_terminal(db_session, monkeypatch):
    monkeypatch.setattr(settings, "kernel_mode", True)
    tid = _make_task(snapshot=SNAP_BIG)
    run_audit_task(tid)
    out = _load(tid)
    assert out["status"] == "decided"
    assert out["level"] == "REJECT"
    assert "HIGH_AMOUNT" in out["problems"]


# ---------------------------------------------------------------------------
# 5. 自定义 SOP（FlowProfile.audit_sop）：极简两节点 SOP 正常跑完
# ---------------------------------------------------------------------------

CUSTOM_SOP = {
    "sop_code": "minimal",
    "entry": "intake",
    "nodes": [
        {"id": "intake", "kind": "rule_eval",
         "required_capabilities": ["rule_query"],
         "transitions": [{"cond": "always", "next": "wb"}]},
        {"id": "wb", "kind": "writeback",
         "required_capabilities": ["writeback_probe"],
         "transitions": [{"cond": "always", "next": "__done__"}]},
    ],
}


def test_custom_sop_from_flow_profile(db_session, monkeypatch):
    monkeypatch.setattr(settings, "kernel_mode", True)
    tid = _make_task(snapshot=SNAP_BIG)
    db = SessionLocal()
    db.add(FlowProfile(flow_code="expense", mode="shadow", audit_sop=CUSTOM_SOP))
    db.commit()
    db.close()

    run_audit_task(tid)
    out = _load(tid)
    assert out["status"] == "decided"
    assert out["level"] == "REJECT"
    assert "HIGH_AMOUNT" in out["problems"]


# ---------------------------------------------------------------------------
# 6. human_gate 帧：awaiting_human 停止后续帧，决策照常产出（T3 边界语义）
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


def test_human_gate_stops_run_but_still_decides(db_session, monkeypatch):
    monkeypatch.setattr(settings, "kernel_mode", True)
    tid = _make_task(snapshot=SNAP_BIG)
    db = SessionLocal()
    db.add(FlowProfile(flow_code="expense", mode="advisory", audit_sop=GATE_SOP))
    db.commit()
    db.close()

    run_audit_task(tid)
    out = _load(tid)
    assert out["status"] == "decided"
    assert out["level"] == "REJECT"  # 矩阵硬违规不因挂起丢失


# ---------------------------------------------------------------------------
# 7. RAG 摘录等价：两条路径下链收到的 policy_excerpts 一致
# ---------------------------------------------------------------------------

def test_rag_excerpts_parity(db_session, monkeypatch):
    from worker.pipeline.rag.embeddings import HashingEmbedding
    from worker.pipeline.rag.retriever import ingest_document

    clauses = [{"clause_no": "8",
                "content": "员工住宿费标准：一线城市每晚不超过600元，"
                           "其他城市不超过450元，超标部分不予报销。"}]
    db = SessionLocal()
    ingest_document(db, HashingEmbedding(), policy_code="travel",
                    title="差旅费管理办法", clauses=clauses)
    db.close()

    snap = {"form": {"amount": 2380, "trip_reason": "住宿费标准是多少"},
            "submitted_at": "2026-06-01T10:00:00+00:00"}
    monkeypatch.setattr(settings, "rag_enabled", True)

    legacy_chain = _StubChain()
    legacy_id = _make_task(snapshot=snap)
    run_audit_task(legacy_id, llm_chain=legacy_chain)
    assert legacy_chain.seen_excerpts, "legacy 应携带制度摘录"

    monkeypatch.setattr(settings, "kernel_mode", True)
    kernel_chain = _StubChain()
    kernel_id = _make_task(snapshot=snap)
    run_audit_task(kernel_id, llm_chain=kernel_chain)
    assert kernel_chain.seen_excerpts, "kernel 应携带制度摘录"
    assert (len(kernel_chain.seen_excerpts)
            == len(legacy_chain.seen_excerpts))
