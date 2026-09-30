"""审核流水线（P0 端到端最小实现）。

事件入站 → 规则引擎求值 → findings/decision 落库 → 任务 decided。
LLM 审核链（S1–S4）与回写执行器在 P1 接入；本模块预留给出的挂载点。
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.entities import (AuditDecision, AuditFinding, AuditTask,
                                 EscalationLog, FlowProfile, LlmCall,
                                 PolicyRule, utcnow)
from app.schemas.canonical import FindingOut
from worker.pipeline.fusion.matrix import fuse
from worker.pipeline.routing.router import route
from worker.pipeline.routing.writeback import execute_writeback
from worker.pipeline.rules.engine import RuleEngine
from worker.pipeline.rules.functions import make_default_functions

logger = logging.getLogger(__name__)


def build_scope(snapshot: dict) -> dict:
    """标准单据快照 → 规则引擎 scope（$form/$attachment/$ctx/$std）。"""
    form = snapshot.get("form", {})
    # 兼容两种快照形态：{"form": {字段...}} 或 {"form": {"fields": {...}}}
    if "fields" in form and isinstance(form["fields"], dict):
        form = form["fields"]
    return {
        "form": form,
        "attachment": {"ocr": snapshot.get("ocr_struct", {})},
        "ctx": snapshot.get("ctx", {}),
        "std": snapshot.get("std", {}),
    }


def seed_demo_rules(db: Session, flow_code: str) -> None:
    """首次运行时种入演示规则（生产中由管理 API 维护，见详细设计 §4.2）。"""
    demo = [
        {
            "rule_code": "high_amount_gate", "rule_name": "大额单据强制人工",
            "problem_code": "HIGH_AMOUNT", "severity": "major",
            "dsl": {
                "rule_code": "high_amount_gate",
                "when": {"op": "gt", "left": "$form.amount", "right": 100000},
                "then": {"problem_code": "HIGH_AMOUNT", "severity": "major",
                         "message": "单据金额 {0} 元超过 10 万元，须人工审核",
                         "message_args": ["$form.amount"], "route_hint": "fin_manager"},
            },
        },
        {
            "rule_code": "missing_trip_reason", "rule_name": "差旅事由必填",
            "problem_code": "MISSING_FIELD", "severity": "minor",
            "dsl": {
                "rule_code": "missing_trip_reason",
                "when": {"op": "any_empty", "left": ["$form.trip_reason"]},
                "then": {"problem_code": "MISSING_FIELD", "severity": "minor",
                         "message": "出差事由缺失，请补充", "route_hint": "applicant"},
            },
        },
    ]
    for d in demo:
        db.add(PolicyRule(flow_code=flow_code, rule_code=d["rule_code"],
                          rule_name=d["rule_name"], problem_code=d["problem_code"],
                          severity=d["severity"], dsl=d["dsl"]))
    db.commit()


def _rag_query(snapshot: dict) -> str:
    """从快照构造 RAG 检索 query：事由/摘要类文本字段优先。"""
    form = snapshot.get("form", {})
    if "fields" in form and isinstance(form["fields"], dict):
        form = form["fields"]
    parts = [str(form.get(k, "")) for k in
             ("trip_reason", "reason", "summary", "purpose", "description")]
    return " ".join(p for p in parts if p) or str(form.get("amount", ""))


def _decide_and_writeback(db: Session, task: AuditTask, profile: FlowProfile, *,
                          findings: list[FindingOut], llm_confidence: float | None,
                          llm_summary: str, writeback_adapter=None) -> str:
    """决策矩阵 → 路由回写 → 任务收尾（legacy 与内核共用的唯一出口）。

    不负责 commit（调用方统一提交）；返回决策级别。
    """
    auto_allowed = (profile.mode in ("semi_auto", "full_auto")
                    and profile.auto_pass_enabled
                    and (profile.amount_cap is None
                         or float((task.snapshot or {}).get("form", {})
                                  .get("amount", 0) or 0) <= profile.amount_cap))

    task.status = "fusion"
    level, reason = fuse(findings, llm_confidence=llm_confidence,
                         auto_allowed=auto_allowed)
    db.add(AuditDecision(task_id=task.id, level=level, confidence=llm_confidence,
                         matrix_reason=reason))

    plan = route(profile, level, findings, summary=llm_summary)
    if plan is not None:
        execute_writeback(db, profile, plan, task_id=task.id,
                          instance_id=task.instance_id, decision_level=level,
                          problem_codes=plan.problem_codes,
                          adapter=writeback_adapter)
    else:
        # shadow 或无需动作：留一条 skipped 记录保证全链路可审计
        db.add(EscalationLog(task_id=task.id, decision_level=level,
                             problem_codes=[f.problem_code for f in findings],
                             writeback_status="skipped_shadow" if profile.mode == "shadow" else "no_action"))

    task.status = "decided"
    task.finished_at = utcnow()
    return level


def run_audit_task(task_id: str, *, llm_chain=None, writeback_adapter=None) -> None:
    """执行单个审核任务。

    执行内核二选一（AUDITOR_KERNEL_MODE）：
      - false（默认）：P0/P1 硬编码流水线（规则 → [LLM 链 S1–S4] → 决策矩阵）；
      - true：v3.0 内核（AuditPlanner SOP 展开 → Harness 逐帧 → 决策矩阵，
        见 app/services/kernel_runner.py，等价迁移见 tests/test_kernel_pipeline.py）。

    llm_chain 参数用于测试注入 stub（LLMaterialAuditChain 同签名对象）；
    生产默认按 settings.llm_enabled 构建真实链。
    """
    from app.config import settings as _s
    from app.models.entities import SessionLocal

    db = SessionLocal()
    try:
        task = db.get(AuditTask, task_id)
        if task is None or task.status not in ("received", "preprocessing"):
            return
        task.status = "rule_eval"

        if db.query(PolicyRule).filter_by(flow_code=task.flow_code).count() == 0:
            seed_demo_rules(db, task.flow_code)

        rules = (db.query(PolicyRule)
                 .filter(PolicyRule.flow_code.in_([task.flow_code, "*"]),
                         PolicyRule.enabled.is_(True))
                 .all())
        engine = RuleEngine(make_default_functions())
        scope = build_scope(task.snapshot or {})

        profile = db.get(FlowProfile, task.flow_code)
        if profile is None:
            profile = FlowProfile(flow_code=task.flow_code, mode="shadow")
            db.add(profile)
            db.flush()

        # ---- v3.0 内核路径（AUDITOR_KERNEL_MODE=true） ----
        if getattr(_s, "kernel_mode", False):
            from app.services.kernel_runner import run_kernel_pipeline
            run_kernel_pipeline(db, task, profile=profile, rules=rules,
                                engine=engine, scope=scope,
                                llm_chain=llm_chain,
                                writeback_adapter=writeback_adapter)
            db.commit()
            return

        findings: list[FindingOut] = []
        for rule in rules:
            res = engine.evaluate_rule(rule.dsl, scope)
            if res.hit and res.finding is not None:
                findings.append(res.finding)

        # ---- P1：LLM 审核链 S1–S4（纯内存计算，异常时不影响已求值的规则 findings） ----
        llm_confidence = None
        llm_traces = []
        llm_summary = ""
        if _s.llm_enabled or llm_chain is not None:
            chain = llm_chain
            if chain is None:
                from worker.pipeline.llm.chain import LLMaterialAuditChain
                from worker.pipeline.llm.client import NewAPIClient
                chain = LLMaterialAuditChain(NewAPIClient())
            try:
                task.status = "llm_chain"
                # P1.3：RAG 制度摘录（按单据提交日过滤生效期），供 S3 引用
                policy_excerpts = None
                if _s.rag_enabled:
                    from worker.pipeline.rag.retriever import hybrid_search
                    from worker.pipeline.rag.embeddings import get_embedding_provider
                    submitted = (task.snapshot or {}).get("submitted_at")
                    submitted_on = None
                    if isinstance(submitted, str) and len(submitted) >= 10:
                        from datetime import date as _date
                        submitted_on = _date.fromisoformat(submitted[:10])
                    rag_res = hybrid_search(db, get_embedding_provider(),
                                            query=_rag_query(task.snapshot or {}),
                                            submitted_on=submitted_on)
                    policy_excerpts = [e.as_dict() for e in rag_res.excerpts]
                chain_result = chain.run(task.snapshot or {}, findings,
                                         policy_excerpts=policy_excerpts)
                findings.extend(chain_result.findings)
                llm_confidence = chain_result.confidence
                llm_summary = chain_result.summary
                llm_traces = chain_result.traces
            except Exception:  # noqa: BLE001 —— LLM 链故障降级为"无 LLM 信号"
                llm_confidence = None
                logger.exception("llm chain failed for task %s, degrade to rules-only",
                                 task_id)

        # ---- 统一落库（先内存聚合再持久化，LLM 降级不回滚规则结果） ----
        for f in findings:
            db.add(AuditFinding(task_id=task.id, problem_code=f.problem_code,
                                severity=f.severity, engine=f.engine, title=f.title,
                                detail=f.detail, evidence=f.evidence,
                                rule_code=f.rule_code))
        for tr in llm_traces:
            db.add(LlmCall(task_id=task.id, step=tr.step, model=tr.model,
                           ok=tr.ok, degraded=tr.degraded,
                           latency_ms=tr.latency_ms, attempts=tr.attempts,
                           error=tr.error[:1000],
                           response_digest=tr.response_digest))

        # ---- 决策矩阵 → 路由回写（legacy 与内核共用出口） ----
        level = _decide_and_writeback(db, task, profile, findings=findings,
                                      llm_confidence=llm_confidence,
                                      llm_summary=llm_summary,
                                      writeback_adapter=writeback_adapter)
        db.commit()
        logger.info("task %s decided: level=%s findings=%d llm_conf=%s",
                    task_id, level, len(findings), llm_confidence)
    except Exception:  # noqa: BLE001 —— 任务级隔离，失败置 failed 不阻塞其他任务
        db.rollback()
        task = db.get(AuditTask, task_id)
        if task is not None:
            task.status = "failed"
            task.error_info = {"stage": "pipeline", "detail": "see logs"}
            db.commit()
        logger.exception("audit task %s failed", task_id)
    finally:
        db.close()
