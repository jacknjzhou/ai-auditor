"""RAG 混合检索 + 转人工路由 + 回写降级 测试。"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.models.entities import (ApprovalEvent, AuditDecision, AuditTask,
                                 EscalationLog, FlowProfile, KnowledgeDoc,
                                 KnowledgeChunk, SessionLocal, init_db)
from app.services.audit_service import run_audit_task
from worker.pipeline.rag.embeddings import HashingEmbedding
from worker.pipeline.rag.retriever import hybrid_search, ingest_document
from worker.pipeline.routing.router import route, match_route
from worker.pipeline.routing.writeback import (FlakyWritebackAdapter,
                                               MockWritebackAdapter,
                                               execute_writeback)


@pytest.fixture()
def db():
    init_db()
    s = SessionLocal()
    yield s
    s.close()


CLAUSES = [
    {"clause_no": "第8条", "content": "员工住宿费标准：一线城市每晚不超过600元，"
                                     "其他城市不超过450元，超标部分不予报销。"},
    {"clause_no": "第12条", "content": "出差伙食补助费按自然日100元定额包干，不再凭票报销。"},
    {"clause_no": "第15条", "content": "报销单据须在出差返回后30日内提交，逾期需部门负责人说明原因。"},
]


def _ingest(db, effective_from=None, effective_to=None):
    return ingest_document(
        db, HashingEmbedding(), policy_code="travel", title="差旅费管理办法",
        version_no=1, effective_from=effective_from, effective_to=effective_to,
        clauses=CLAUSES)


# ---------- RAG ----------

def test_ingest_and_search_relevant_clause_first(db):
    _ingest(db)
    res = hybrid_search(db, HashingEmbedding(), query="住宿费超标怎么算",
                        submitted_on=date(2026, 6, 1))
    assert not res.degraded
    assert len(res.excerpts) >= 1
    assert "住宿" in res.excerpts[0].text
    assert res.excerpts[0].policy_code == "travel"


def test_effective_date_filters_by_submitted_date(db):
    """制度生效期按【单据提交日】过滤——2025年提交的单据查不到2026生效的制度。"""
    _ingest(db, effective_from=datetime(2026, 1, 1, tzinfo=timezone.utc))
    old = hybrid_search(db, HashingEmbedding(), query="住宿费",
                        submitted_on=date(2025, 6, 1))
    assert old.degraded  # 提交日早于生效日 → 无可用制度
    new = hybrid_search(db, HashingEmbedding(), query="住宿费",
                        submitted_on=date(2026, 6, 1))
    assert not new.degraded


def test_expired_doc_excluded(db):
    _ingest(db, effective_to=datetime(2026, 2, 1, tzinfo=timezone.utc))
    res = hybrid_search(db, HashingEmbedding(), query="住宿费",
                        submitted_on=date(2026, 6, 1))
    assert res.degraded


# ---------- 路由 ----------

def _profile(mode="advisory", route_table=None, tier="FULL_API", **kw):
    return FlowProfile(flow_code="fx", mode=mode,
                       route_table=route_table or [], writeback_tier=tier, **kw)


ROUTE_TABLE = [
    {"problem_codes": ["MISSING_RECEIPT", "MISSING_FIELD"], "target_node": "applicant",
     "action": "RETURN", "priority": 2},
    {"problem_codes": ["OVER_BUDGET"], "target_node": "fin_manager",
     "action": "ESCALATE", "priority": 1},
]


def test_route_shadow_returns_none():
    assert route(_profile(mode="shadow"), "REJECT", []) is None


def test_route_priority_and_codes():
    p = _profile(route_table=ROUTE_TABLE)
    assert match_route(p, ["MISSING_RECEIPT"]) == ("RETURN", "applicant")
    assert match_route(p, ["OVER_BUDGET"]) == ("ESCALATE", "fin_manager")
    # 两个问题码同现 → priority 1 的 ESCALATE 优先
    assert match_route(p, ["MISSING_RECEIPT", "OVER_BUDGET"]) == ("ESCALATE", "fin_manager")
    assert match_route(p, ["UNKNOWN_CODE"]) is None


def test_route_advisory_comment_only_when_findings():
    from app.schemas.canonical import FindingOut
    p = _profile()
    f = FindingOut(problem_code="MISSING_FIELD", severity="minor", title="缺", detail="")
    plan = route(p, "ADVISORY", [f])
    assert plan is not None and plan.action == "COMMENT"
    assert route(p, "ADVISORY", []) is None  # 无问题不打扰


# ---------- 回写降级 ----------

def test_writeback_degrades_on_tier_failure(db):
    from worker.pipeline.routing.router import RoutePlan
    p = _profile(tier="FULL_API")
    plan = RoutePlan(action="RETURN", target_node="applicant",
                     problem_codes=["MISSING_RECEIPT"], comment="缺发票")
    adapter = FlakyWritebackAdapter({"FULL_API"})  # FULL API 挂 → 降级 COMMENT_ONLY
    outcome = execute_writeback(db, p, plan, task_id="t-wb", instance_id="I1",
                                decision_level="REJECT",
                                problem_codes=["MISSING_RECEIPT"], adapter=adapter)
    assert outcome.status == "degraded"
    assert outcome.tier_used == "COMMENT_ONLY"
    assert [a["tier"] for a in outcome.attempts] == ["FULL_API", "COMMENT_ONLY"]
    log = db.query(EscalationLog).filter_by(task_id="t-wb").first()
    assert log.writeback_status == "degraded"


def test_writeback_all_tiers_fail(db):
    from worker.pipeline.routing.router import RoutePlan
    p = _profile(tier="FULL_API")
    plan = RoutePlan(action="RETURN", target_node="applicant", problem_codes=[])
    adapter = FlakyWritebackAdapter({"FULL_API", "COMMENT_ONLY", "IM_ONLY"})
    outcome = execute_writeback(db, p, plan, task_id="t-wb2", instance_id="I1",
                                decision_level="REJECT", problem_codes=[],
                                adapter=adapter)
    assert outcome.status == "failed"


# ---------- 端到端集成 ----------

def _make_task(db, flow_code, form):
    ev = ApprovalEvent(source_code="oa-a8", instance_id=f"I-{flow_code}-{form}",
                       node_code="fin_review", event_type="NODE_ARRIVED",
                       idempotency_key=f"rt-{flow_code}-{form}", version_no=1,
                       raw_payload={})
    db.add(ev)
    db.flush()
    task = AuditTask(event_id=ev.id, source_code="oa-a8", instance_id=ev.instance_id,
                     node_code="fin_review", flow_code=flow_code,
                     snapshot={"form": form, "attachments": []},
                     status="received", mode="shadow")
    db.add(task)
    db.commit()
    return task.id


def test_semi_auto_clean_form_auto_pass(db):
    """半自动 + 干净单据 + 金额在限额内 → 自动通过 PASS。"""
    db.add(FlowProfile(flow_code="flow_pass", mode="semi_auto",
                       auto_pass_enabled=True, amount_cap=1000,
                       route_table=ROUTE_TABLE))
    db.commit()
    task_id = _make_task(db, "flow_pass", {"amount": 500, "trip_reason": "出差"})
    run_audit_task(task_id, writeback_adapter=MockWritebackAdapter())
    dec = db.query(AuditDecision).filter_by(task_id=task_id).first()
    assert dec.level == "PASS"
    log = db.query(EscalationLog).filter_by(task_id=task_id).first()
    assert log.writeback_status in ("no_action", "skipped_shadow")


def test_amount_cap_blocks_auto_pass(db):
    """金额超过自动通过上限 → auto_allowed=False → ADVISORY。"""
    db.add(FlowProfile(flow_code="flow_cap", mode="semi_auto",
                       auto_pass_enabled=True, amount_cap=100,
                       route_table=ROUTE_TABLE))
    db.commit()
    task_id = _make_task(db, "flow_cap", {"amount": 500, "trip_reason": "出差"})
    run_audit_task(task_id, writeback_adapter=MockWritebackAdapter())
    dec = db.query(AuditDecision).filter_by(task_id=task_id).first()
    assert dec.level == "ADVISORY"


def test_reject_routes_back_to_applicant(db):
    """大额硬违规 → REJECT → 路由表打回提单人，mock 回写 applied。"""
    db.add(FlowProfile(flow_code="flow_rej", mode="advisory",
                       route_table=ROUTE_TABLE, writeback_tier="FULL_API"))
    db.commit()
    task_id = _make_task(db, "flow_rej", {"amount": 500000, "trip_reason": "出差"})
    adapter = MockWritebackAdapter()
    run_audit_task(task_id, writeback_adapter=adapter)
    dec = db.query(AuditDecision).filter_by(task_id=task_id).first()
    assert dec.level == "REJECT"
    log = db.query(EscalationLog).filter_by(task_id=task_id).first()
    assert log.action == "RETURN" and log.target_node == "applicant"
    assert log.writeback_status == "applied"
    assert adapter.calls[0]["target_node"] == "applicant"
