"""内核执行器（P3.0-T3）—— AuditPlanner + HarnessAgent 驱动的审核 run。

用 SOP 状态机（内置等价 SOP 或 FlowProfile.audit_sop 自定义 SOP）替换
P0/P1 硬编码流水线，等价迁移口径（设计 §6 保留资产映射）：

- 规则求值：runner 预求值（与 legacy 同一 engine/scope），结果经 rule_query
  能力进入 intake 帧的 finding 流（帧协议内闭环，Harness 收集）；
- LLM 链：llm_verify/llm_assess 能力按 SOP 节点触发，同 run 共享一次调用缓存
  （ctx.cache），traces 与 legacy 同源写入 llm_call 表；
- RAG 制度摘录：沿用 legacy 预取口径——rag_enabled 时 runner 预取并注入
  ctx.scratch，保证 llm_verify 首次触发链调用时即携带 excerpts（与 P1 等价；
  内置 SOP 中 knowledge_check 帧的检索结果追加进 scratch，供后续节点复用）；
- 运行时条件（on_*）：Planner 静态展开止于首个运行时条件；runner 依据帧结果
  解析 run_state 后经 Planner.expand 二次展开（hard_violation=True → 内置 SOP
  走 __reject__ 终态，与决策矩阵 REJECT 结论互为印证）；
- 决策融合/路由回写：复用 audit_service._decide_and_writeback，与 legacy
  完全同一代码路径（决策矩阵仍是最终确定性 gate，设计 §3 差异③）。

T3 边界（P1.5 run 挂起-恢复模型接入前）：
- human_gate 帧 → finish awaiting_human → runner 停止后续帧、照常出决策
  （矩阵按无 LLM 信号/发现问题升级人工），不阻塞流水线；
- failure_policy=halt 的帧失败 → 同样停止后续帧并出决策。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.entities import AuditTask, FlowProfile, LlmCall
from app.schemas.canonical import FindingOut
from worker.pipeline.kernel.capabilities import (CapabilityContext,
                                                 default_registry)
from worker.pipeline.kernel import events as _ev
from worker.pipeline.kernel.events import EventRecorder
from worker.pipeline.kernel.harness import HarnessAgent
from worker.pipeline.kernel.planner import (RESERVED, AuditPlanner, AuditSOP)
from worker.pipeline.rules.engine import RuleEngine

logger = logging.getLogger(__name__)

# 各帧 kind 的默认强制能力调用脚本（None = 入参运行时构造）
_FRAME_TOOL: dict[str, tuple[str, dict | None]] = {
    "rule_eval": ("rule_query", {}),
    "llm_verify": ("llm_verify", {}),
    "knowledge_check": ("knowledge_search", None),  # {"query": rag_query}
    "risk_assess": ("llm_assess", {}),
    "writeback": ("writeback_probe", {}),
}


# ---------------------------------------------------------------------------
# 确定性帧 actor：调用节点强制能力 → 依据观察收尾（零 LLM 决策，协议内闭环）
# ---------------------------------------------------------------------------

def _deterministic_actor(frame, rag_query: str):
    """内置确定性 actor：每帧恰好调用一次强制能力，随后 finish。

    - human_gate 帧：直接 finish awaiting_human（人工恢复属 P1.5）；
    - llm_assess 帧：从观察中提取置信度/风险标记/摘要，经 structured_result
      带入 FrameOutcome（Composer 消费）；
    - 强制能力失败时照常 finish completed → 被完成门槛拦截进入修复链，
      修复超限帧置 failed（failure_policy=degrade 时 run 继续降级）。
    """
    called = False

    def actor(turns: list[dict[str, str]]) -> Any:
        nonlocal called
        if frame.kind == "human_gate":
            return {"action": "finish", "status": "awaiting_human"}

        if not called:
            called = True
            spec = _FRAME_TOOL.get(frame.kind)
            if spec is None:  # 未知 kind：无工具直接收尾
                return {"action": "finish", "status": "completed"}
            name, args = spec
            if args is None:  # knowledge_search 需要 query
                args = {"query": rag_query}
            return {"action": "tool", "tool_name": name, "arguments": args}

        # 已调用 → 从观察提取 llm_assess 信号后收尾
        structured: dict | None = None
        summary = ""
        for t in reversed(turns):
            if t.get("role") != "observation":
                continue
            try:
                data = json.loads(t["content"])
            except (json.JSONDecodeError, TypeError):
                continue
            if data.get("tool") == "llm_assess" and data.get("ok"):
                res = data.get("result") or {}
                structured = {"confidence": res.get("confidence"),
                              "risk_flags": list(res.get("risk_flags") or [])}
                summary = str(res.get("summary") or "")
                break
        action: dict[str, Any] = {"action": "finish", "status": "completed",
                                  "task_summary": summary}
        if structured is not None:
            action["structured_result"] = structured
        return action

    return actor


# ---------------------------------------------------------------------------
# 运行时条件解析
# ---------------------------------------------------------------------------

def _run_state(findings: list[FindingOut]) -> dict[str, bool]:
    """帧结果 → 运行时条件真值（内置 SOP 消费 hard_violation）。"""
    severities = {f.severity for f in findings}
    return {
        "hard_violation": bool(severities & {"major", "critical"}),
        # T3：自动路径无人工挂起；P1.5 挂起-恢复模型接入后由 human_task 驱动
        "awaiting_resolved": True,
        "low_confidence": False,
    }


def _resolve_next(sop: AuditSOP, node_id: str, run_state: dict[str, bool],
                  engine: RuleEngine, scope: dict) -> str | None:
    """解析停止节点的转移：返回下一节点 id / 保留字 / None（无可满足转移）。

    转移形态与 Planner 一致：always / on_* 运行时命名条件 / 规则 DSL clause
    （复用同一 engine，fail-safe 语义一致——缺失变量为确定性 False）。
    """
    node = sop.nodes.get(node_id)
    if node is None:
        return None
    for tr in node.transitions:
        cond = tr.get("cond")
        if cond == "always":
            return tr.get("next")
        if isinstance(cond, str) and cond.startswith("on_"):
            if run_state.get(cond[3:]) is True:
                return tr.get("next")
            continue
        if isinstance(cond, dict):
            try:
                if engine.eval_clause(cond, scope, []):
                    return tr.get("next")
            except Exception:  # noqa: BLE001 —— 与 Planner 同语义：不可判定即跳过
                continue
            continue
    return None


# ---------------------------------------------------------------------------
# RAG 预取（与 legacy 完全同口径，保证链调用入参等价）
# ---------------------------------------------------------------------------

def _prefetch_excerpts(db: Session, task: AuditTask) -> list[dict] | None:
    from app.config import settings as _s
    from app.services.audit_service import _rag_query

    if not _s.rag_enabled:
        return None
    from datetime import date as _date

    from worker.pipeline.rag.embeddings import get_embedding_provider
    from worker.pipeline.rag.retriever import hybrid_search

    submitted = (task.snapshot or {}).get("submitted_at")
    submitted_on = None
    if isinstance(submitted, str) and len(submitted) >= 10:
        submitted_on = _date.fromisoformat(submitted[:10])
    res = hybrid_search(db, get_embedding_provider(),
                        query=_rag_query(task.snapshot or {}),
                        submitted_on=submitted_on)
    return [e.as_dict() for e in res.excerpts]


def _submitted_on(task: AuditTask):
    submitted = (task.snapshot or {}).get("submitted_at")
    if isinstance(submitted, str) and len(submitted) >= 10:
        from datetime import date as _date
        return _date.fromisoformat(submitted[:10])
    return None


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def _load_sop(profile: FlowProfile) -> AuditSOP:
    dsl = getattr(profile, "audit_sop", None)
    if dsl:
        return AuditSOP(dsl)
    return AuditSOP.builtin()  # 空配置零迁移承诺


def run_kernel_pipeline(db: Session, task: AuditTask, *, profile: FlowProfile,
                        rules: list, engine: RuleEngine, scope: dict,
                        llm_chain=None, writeback_adapter=None) -> None:
    """内核路径：Planner 展开 → Harness 逐帧执行 → 运行时条件二次展开 → 决策回写。

    落库契约与 legacy 完全一致：findings/decision/escalation_log/llm_call，
    最终 task.status=decided；异常由 run_audit_task 外层捕获置 failed。
    """
    from app.config import settings as _s
    from app.services.audit_service import _decide_and_writeback, _rag_query
    from worker.pipeline.rag.embeddings import get_embedding_provider

    chain = llm_chain
    if chain is None and _s.llm_enabled:
        from worker.pipeline.llm.chain import LLMaterialAuditChain
        from worker.pipeline.llm.client import NewAPIClient
        chain = LLMaterialAuditChain(NewAPIClient())

    # ---- T4：run_event 全链路埋点（内存累积，finally 统一落库） ----
    rec = EventRecorder()

    # ---- 规则预求值（与 legacy 同一 engine/scope；结果经能力进入帧协议） ----
    rule_findings: list[FindingOut] = []
    for rule in rules:
        res = engine.evaluate_rule(rule.dsl, scope)
        if res.hit and res.finding is not None:
            rule_findings.append(res.finding)

    excerpts = _prefetch_excerpts(db, task)

    ctx = CapabilityContext(
        db=db,
        snapshot=task.snapshot or {},
        scope=scope,
        flow_code=task.flow_code,
        submitted_on=_submitted_on(task),
        rule_findings=list(rule_findings),
        llm_chain=chain,
        embedding_provider=get_embedding_provider() if _s.rag_enabled else None,
        writeback_tier=profile.writeback_tier,
    )
    if excerpts:
        ctx.scratch["policy_excerpts"] = list(excerpts)

    harness = HarnessAgent(default_registry(), on_event=rec.emit)
    planner = AuditPlanner()
    sop = _load_sop(profile)
    rag_query = _rag_query(task.snapshot or {})

    all_findings: list[FindingOut] = []
    confidence: float | None = None
    summary = ""
    executed_ids: list[str] = []

    def _run_frames(frames) -> str:
        """顺序执行帧；返回 ok | awaiting_human | halt（后两者停止后续帧）。"""
        nonlocal confidence, summary
        for frame in frames:
            if frame.kind in ("llm_verify", "risk_assess"):
                task.status = "llm_chain"
            rec.emit(_ev.EVENT_FRAME_STARTED, frame_id=frame.frame_id,
                     kind=frame.kind, node_id=frame.node_id)
            outcome = harness.run(frame, ctx, _deterministic_actor(frame, rag_query))
            executed_ids.append(frame.frame_id)
            all_findings.extend(outcome.findings)
            # capability_call 留痕（budget_left = 预算余量，仅实际扣减的调用有值）
            consumed = 0
            for call in outcome.calls:
                budget_left = None
                if call.budget_consumed:
                    consumed += 1
                    budget_left = frame.requirement.knowledge_budget - consumed
                rec.record_capability(frame.frame_id, call, budget_left)
            rec.emit(_ev.EVENT_FRAME_FINISHED, frame_id=frame.frame_id,
                     status=outcome.status, n_findings=len(outcome.findings),
                     n_calls=len(outcome.calls), degraded=outcome.degraded,
                     halt_run=outcome.halt_run, loops=outcome.loops,
                     protocol_repairs=outcome.protocol_repairs)
            if outcome.confidence is not None:
                confidence = outcome.confidence
            if outcome.summary:
                summary = outcome.summary
            if outcome.status == "awaiting_human":
                return "awaiting_human"
            if outcome.status == "failed" and frame.requirement.failure_policy == "halt":
                return "halt"
        return "ok"

    def _emit_planned(frames) -> None:
        for f in frames:
            rec.emit(_ev.EVENT_FRAME_PLANNED, frame_id=f.frame_id, kind=f.kind,
                     node_id=f.node_id,
                     required_capabilities=list(f.requirement.required_capabilities),
                     optional_capabilities=list(f.requirement.optional_capabilities))

    try:
        result = planner.plan(sop, task.id, scope)
        _emit_planned(result.frames)
        state = _run_frames(result.frames)

        # ---- 运行时条件：解析 → 二次展开（内置 SOP 会在此走完 policy/risk/writeback） ----
        guard = 0
        while state == "ok" and result.continuation and guard < 16:
            guard += 1
            stop_node_id = next((f.node_id for f in result.frames
                                 if f.frame_id == result.continuation_after), None)
            if stop_node_id is None:
                logger.warning("run %s continuation 无对应帧: %s",
                               task.id, result.continuation_after)
                break
            nxt = _resolve_next(sop, stop_node_id, _run_state(all_findings),
                                engine, scope)
            if nxt is None:
                logger.warning("run %s 节点 %s 运行时条件均未满足，提前终结",
                               task.id, stop_node_id)
                break
            if nxt in RESERVED:
                result.terminal = nxt
                result.continuation = None
                logger.info("run %s 命中终态 %s（节点 %s）", task.id, nxt, stop_node_id)
                break
            result = planner.expand(sop, task.id, nxt,
                                    seq_start=len(executed_ids) + 1,
                                    depends_on=[result.continuation_after],
                                    scope=scope)
            _emit_planned(result.frames)
            state = _run_frames(result.frames)

        # ---- findings 落库（与 legacy"统一落库"口径一致） ----
        from app.models.entities import AuditFinding, EscalationLog
        for f in all_findings:
            db.add(AuditFinding(task_id=task.id, problem_code=f.problem_code,
                                severity=f.severity, engine=f.engine, title=f.title,
                                detail=f.detail, evidence=f.evidence,
                                rule_code=f.rule_code))

        # ---- LLM 调用留痕（与 legacy 同源：链结果缓存中的 traces） ----
        chain_result = ctx.cache_get("llm_chain_result")
        if chain_result is not None:
            for tr in chain_result.traces:
                db.add(LlmCall(task_id=task.id, step=tr.step, model=tr.model,
                               ok=tr.ok, degraded=tr.degraded,
                               latency_ms=tr.latency_ms, attempts=tr.attempts,
                               error=tr.error[:1000],
                               response_digest=tr.response_digest))

        # ---- 决策矩阵 + 路由回写（与 legacy 同一代码路径） ----
        level = _decide_and_writeback(db, task, profile, findings=all_findings,
                                      llm_confidence=confidence, llm_summary=summary,
                                      writeback_adapter=writeback_adapter)

        # ---- T4：composed / writeback_applied 事件（回写结果取本次 run 的 escalation 记录） ----
        db.flush()  # autoflush=False：让 _decide_and_writeback 挂起的 escalation 行对本查询可见
        esc = (db.query(EscalationLog).filter_by(task_id=task.id)
               .order_by(EscalationLog.id.desc()).first())
        rec.emit(_ev.EVENT_COMPOSED, level=level, terminal=result.terminal,
                 n_findings=len(all_findings), confidence=confidence)
        rec.emit(_ev.EVENT_WRITEBACK_APPLIED, level=level,
                 writeback_status=esc.writeback_status if esc else "none",
                 action=esc.action if esc else "",
                 target_node=esc.target_node if esc else "")
        logger.info("kernel run %s decided: level=%s frames=%d findings=%d "
                    "terminal=%s conf=%s",
                    task.id, level, len(executed_ids), len(all_findings),
                    result.terminal, confidence)
    finally:
        # 事件流旁路落库：即使主流程异常也不吞异常、不阻断外层 failed 处理
        try:
            rec.flush(db, task.id)
        except Exception:  # noqa: BLE001
            logger.warning("run %s run_event 落库失败", task.id, exc_info=True)
