"""内核执行器（P3.0-T3/T4/T5）—— AuditPlanner + HarnessAgent 驱动的审核 run。

用 SOP 状态机（内置等价 SOP 或 FlowProfile.audit_sop 自定义 SOP）替换
P0/P1 硬编码流水线，等价迁移口径（设计 §6 保留资产映射）：

- 规则求值：runner 预求值（与 legacy 同一 engine/scope），结果经 rule_query
  能力进入 intake 帧的 finding 流（帧协议内闭环，Harness 收集）；
- LLM 链：llm_verify/llm_assess 能力按 SOP 节点触发，同 run 共享一次调用缓存
  （ctx.cache），traces 与 legacy 同源写入 llm_call 表；
- RAG 制度摘录：沿用 legacy 预取口径——rag_enabled 时 runner 预取并注入
  ctx.scratch，保证 llm_verify 首次触发链调用时即携带 excerpts（与 P1 等价）；
- 运行时条件（on_*）：Planner 静态展开止于首个运行时条件；runner 依据帧结果
  解析 run_state 后经 Planner.expand 二次展开（hard_violation=True → 内置 SOP
  走 __reject__ 终态，与决策矩阵 REJECT 结论互为印证）；
- 决策融合/路由回写：复用 audit_service._decide_and_writeback，与 legacy
  完全同一代码路径（决策矩阵仍是机器结论的最终确定性 gate，设计 §3 差异③）。

T5 挂起-恢复（v2.1 §1 / v3.0 §6）：
- human_gate 帧 → finish awaiting_human → 写 human_task（resume_token 幂等）、
  task.status=awaiting_human、run 挂起（不出决策）；一次 run 最多
  max_suspend_rounds 轮挂起，超限强制出决策（防 human_gate 死循环）；
- resume_kernel_run：COMMENT（全帧复用续跑）/ SUPPLY_MATERIAL（patch 合并 +
  规则全量重算 + 帧指纹复用：材料类帧重跑、制度摘录未变帧 reused）/
  OVERRIDE_*（不经流水线直写终审，matrix_reason.cell=human_override）；
- 帧档案 task_frame 持久化（outcome + 指纹），是增量重审的复用基础；
- 链结果随 human_task.run_cache 瞬态保存，恢复时还原 ctx.cache，
  下游 LLM 帧零重复调用（traces 持久化以对象标记去重）。
"""
from __future__ import annotations

import json
import logging
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.models.entities import (AuditTask, FlowProfile, HumanTask, LlmCall,
                                 TaskFrameRecord, utcnow)
from app.schemas.canonical import FindingOut
from worker.pipeline.kernel import events as _ev
from worker.pipeline.kernel.capabilities import (CapabilityContext,
                                                 default_registry, digest)
from worker.pipeline.kernel.composer import ComposedDecision, DecisionComposer
from worker.pipeline.kernel.events import EventRecorder
from worker.pipeline.kernel.frames import TaskFrame
from worker.pipeline.kernel.harness import FrameOutcome, HarnessAgent
from worker.pipeline.kernel.planner import RESERVED, AuditPlanner, AuditSOP
from worker.pipeline.rules.engine import RuleEngine

logger = logging.getLogger(__name__)

# 人工动作白名单（v2.1 §1.3）
RESUME_ACTIONS = ("SUPPLY_MATERIAL", "OVERRIDE_PASS", "OVERRIDE_REJECT", "COMMENT")
OVERRIDE_LEVELS = {"OVERRIDE_PASS": "PASS", "OVERRIDE_REJECT": "REJECT"}

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

    - human_gate 帧：直接 finish awaiting_human（挂起-恢复由 T5 resume 承接）；
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
        # 挂起-恢复（T5）：恢复后 awaiting_resolved 恒真（人工已决议）
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
# 公共准备 / 持久化辅助（run 与 resume 共用）
# ---------------------------------------------------------------------------

def _load_sop(profile: FlowProfile) -> AuditSOP:
    dsl = getattr(profile, "audit_sop", None)
    if dsl:
        return AuditSOP(dsl)
    return AuditSOP.builtin()  # 空配置零迁移承诺


def _build_chain(llm_chain, settings):
    if llm_chain is not None:
        return llm_chain
    if settings.llm_enabled:
        from worker.pipeline.llm.chain import LLMaterialAuditChain
        from worker.pipeline.llm.client import NewAPIClient
        return LLMaterialAuditChain(NewAPIClient())
    return None


def _eval_rules(engine: RuleEngine, rules: list, scope: dict) -> list[FindingOut]:
    """规则预求值（与 legacy 同一 engine/scope；结果经能力进入帧协议）。"""
    out: list[FindingOut] = []
    for rule in rules:
        res = engine.evaluate_rule(rule.dsl, scope)
        if res.hit and res.finding is not None:
            out.append(res.finding)
    return out


def _build_ctx(db: Session, task: AuditTask, profile: FlowProfile, *, scope: dict,
               chain, rule_findings: list[FindingOut],
               excerpts: list[dict] | None) -> CapabilityContext:
    from app.config import settings as _s
    from worker.pipeline.rag.embeddings import get_embedding_provider

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
    return ctx


def _frame_fingerprint(kind: str, node_id: str, *, scope: dict, snapshot: dict,
                       rule_findings: list[FindingOut], excerpts: list[dict] | None,
                       rag_query: str, writeback_tier: str) -> str:
    """帧输入指纹（T5 增量重审判定）——只覆盖该 kind 真实消费的输入。

    - rule_eval：整个 scope（规则层全量重算口径，任何表单变更都触发重跑）；
    - llm_verify：snapshot（材料类 S1/S2 随单据内容变化）；
    - knowledge_check：rag query（知识库内容版本未入指纹，P4 版本化后补齐）；
    - risk_assess：规则结论 + 制度摘录（S3 复用口径：摘录未变即可复用）；
    - writeback：回写档位。
    """
    if kind == "rule_eval":
        inp: dict[str, Any] = {"scope": scope}
    elif kind == "llm_verify":
        inp = {"snapshot": snapshot}
    elif kind == "knowledge_check":
        inp = {"query": rag_query}
    elif kind == "risk_assess":
        inp = {"rules": sorted((f.problem_code, f.severity) for f in rule_findings),
               "excerpts": digest(excerpts or [])}
    elif kind == "writeback":
        inp = {"tier": writeback_tier}
    else:
        inp = {}
    return digest({"kind": kind, "node_id": node_id, "in": inp})


@dataclass
class _RunSink:
    """run 段内聚合状态（run 与 resume 共用）。

    findings/confidence/summary 由帧产出列表派生（DecisionComposer 归一前的
    原始账本），保证 run 首轮与恢复续跑（含 reused 帧）口径完全一致。
    """

    outcomes: list[FrameOutcome] = field(default_factory=list)  # 帧产出（顺序即执行/复用顺序）
    executed: list[str] = field(default_factory=list)
    reused: list[str] = field(default_factory=list)
    seq_floor: int = 1  # 下一个可用帧 seq（> 已占用的最大 seq，防 (run_id,seq) 冲突）
    awaiting_frame: TaskFrame | None = None
    awaiting_outcome: FrameOutcome | None = None
    skip: set[tuple[str, str]] = field(default_factory=set)  # (node_id, kind) 已复用

    @property
    def findings(self) -> list[FindingOut]:
        return [f for oc in self.outcomes for f in oc.findings]


def _make_frame_runner(db: Session, task: AuditTask, ctx: CapabilityContext,
                       harness: HarnessAgent, rec: EventRecorder, sink: _RunSink,
                       *, rag_query: str, excerpts: list[dict] | None):
    """构造 _run_frames 闭包：帧执行 + 埋点 + task_frame 持久化 + 聚合。

    run 首轮与 resume 续跑共用同一实现，保证事件序列/帧档案口径一致。
    """

    def _run_frames(frames) -> str:
        """顺序执行帧；返回 ok | awaiting_human | halt（后两者停止后续帧）。"""
        for frame in frames:
            sink.seq_floor = max(sink.seq_floor, frame.seq + 1)
            if (frame.node_id, frame.kind) in sink.skip:
                continue  # T5：指纹未变帧已复用上轮结果，不重复执行
            if frame.kind in ("llm_verify", "risk_assess"):
                task.status = "llm_chain"
            rec.emit(_ev.EVENT_FRAME_STARTED, frame_id=frame.frame_id,
                     kind=frame.kind, node_id=frame.node_id)
            outcome = harness.run(frame, ctx, _deterministic_actor(frame, rag_query))
            sink.executed.append(frame.frame_id)
            sink.outcomes.append(outcome)

            # 帧档案落库（outcome + 指纹；增量重审复用基础）
            fp = _frame_fingerprint(frame.kind, frame.node_id, scope=ctx.scope,
                                    snapshot=ctx.snapshot,
                                    rule_findings=ctx.rule_findings,
                                    excerpts=excerpts, rag_query=rag_query,
                                    writeback_tier=ctx.writeback_tier)
            db.add(TaskFrameRecord(
                run_id=task.id, seq=frame.seq, frame_id=frame.frame_id,
                node_id=frame.node_id, kind=frame.kind, status=outcome.status,
                requirement=frame.requirement.to_dict(),
                depends_on=list(frame.depends_on or []),
                outcome={**outcome.to_dict(), "fingerprint": fp},
                finished_at=utcnow()))

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
            if outcome.status == "awaiting_human":
                sink.awaiting_frame, sink.awaiting_outcome = frame, outcome
                return "awaiting_human"
            if outcome.status == "failed" and frame.requirement.failure_policy == "halt":
                return "halt"
        return "ok"

    return _run_frames


def _drain_continuations(sop: AuditSOP, planner: AuditPlanner, task_id: str,
                         engine: RuleEngine, scope: dict, sink: _RunSink,
                         run_frames, rec: EventRecorder,
                         result, state: str) -> str:
    """运行时条件：解析 → 二次展开循环（静态展开止于首个 on_* 条件）。"""
    guard = 0
    while state == "ok" and result.continuation and guard < 16:
        guard += 1
        stop_node_id = next((f.node_id for f in result.frames
                             if f.frame_id == result.continuation_after), None)
        if stop_node_id is None:
            logger.warning("run %s continuation 无对应帧: %s",
                           task_id, result.continuation_after)
            break
        nxt = _resolve_next(sop, stop_node_id, _run_state(sink.findings),
                            engine, scope)
        if nxt is None:
            logger.warning("run %s 节点 %s 运行时条件均未满足，提前终结",
                           task_id, stop_node_id)
            break
        if nxt in RESERVED:
            result.terminal = nxt
            result.continuation = None
            logger.info("run %s 命中终态 %s（节点 %s）", task_id, nxt, stop_node_id)
            break
        result = planner.expand(sop, task_id, nxt,
                                seq_start=sink.seq_floor,
                                depends_on=[result.continuation_after],
                                scope=scope)
        for f in result.frames:
            rec.emit(_ev.EVENT_FRAME_PLANNED, frame_id=f.frame_id, kind=f.kind,
                     node_id=f.node_id,
                     required_capabilities=list(f.requirement.required_capabilities),
                     optional_capabilities=list(f.requirement.optional_capabilities))
        state = run_frames(result.frames)
    return state


def _persist_findings(db: Session, task: AuditTask, findings: list[FindingOut]) -> None:
    """findings 落库（决策时点一次性写入；挂起期暂存于 task_frame.outcome）。"""
    from app.models.entities import AuditFinding
    for f in findings:
        db.add(AuditFinding(task_id=task.id, problem_code=f.problem_code,
                            severity=f.severity, engine=f.engine, title=f.title,
                            detail=f.detail, evidence=f.evidence,
                            rule_code=f.rule_code))


def _persist_chain_traces(db: Session, task: AuditTask, chain_result) -> None:
    """LLM 调用留痕（与 legacy 同源：链结果缓存中的 traces）。

    以对象标记去重：挂起时点已持久化过的链结果（_traces_persisted）不重写。
    """
    if chain_result is None:
        return
    if getattr(chain_result, "_traces_persisted", False):
        return
    for tr in chain_result.traces:
        db.add(LlmCall(task_id=task.id, step=tr.step, model=tr.model,
                       ok=tr.ok, degraded=tr.degraded,
                       latency_ms=tr.latency_ms, attempts=tr.attempts,
                       error=tr.error[:1000],
                       response_digest=tr.response_digest))
    chain_result._traces_persisted = True


def _persist_run_cache(ctx: CapabilityContext) -> dict | None:
    """链结果 → run 级瞬态缓存（挂起时保存，恢复时还原 ctx.cache）。"""
    cr = ctx.cache_get("llm_chain_result")
    if cr is None:
        return None
    return {"llm_chain_result": cr.to_dict(),
            "traces_persisted": bool(getattr(cr, "_traces_persisted", False))}


def _try_suspend(db: Session, task: AuditTask, sink: _RunSink, rec: EventRecorder,
                 ctx: CapabilityContext) -> bool:
    """挂起：写 human_task + task.status=awaiting_human。

    超过 max_suspend_rounds 轮次上限则返回 False（调用方强制出决策，防死循环）。
    """
    from app.config import settings as _s
    from app.models.entities import RunEvent

    rounds = db.query(HumanTask).filter_by(run_id=task.id).count()
    if rounds >= _s.max_suspend_rounds:
        logger.warning("run %s 达到挂起轮次上限 %d，强制出决策",
                       task.id, _s.max_suspend_rounds)
        return False

    frame = sink.awaiting_frame
    ht = HumanTask(
        run_id=task.id,
        seq=rec.current_seq,  # 挂起时的 run_event seq（awaiting_human 事件）
        frame_id=frame.frame_id,
        node_id=frame.node_id,
        problem_codes=sorted({f.problem_code for f in sink.findings}),
        assignee_role="",
        options=list(RESUME_ACTIONS),
        resume_token=_uuid.uuid4().hex,
        run_cache=_persist_run_cache(ctx),
    )
    db.add(ht)
    task.status = "awaiting_human"
    # 挂起时点持久化链 traces（round-N 新鲜链结果；已持久化过的被标记去重）
    _persist_chain_traces(db, task, ctx.cache_get("llm_chain_result"))
    logger.info("run %s 挂起于节点 %s（round %d）", task.id, frame.node_id, rounds + 1)
    return True


def _compose(sink: _RunSink, terminal: str | None) -> ComposedDecision:
    """帧产出 → 归一化决策输入（第三层 Composer，设计 §3）。"""
    return DecisionComposer().compose(
        sink.outcomes, terminal=terminal, executed_frames=sink.executed,
        reused_frames=sink.reused)


def _decide_and_emit(db: Session, task: AuditTask, profile: FlowProfile,
                     rec: EventRecorder, *, composed: ComposedDecision,
                     terminal: str | None,
                     writeback_adapter=None, override_level: str | None = None,
                     extra_reason: dict | None = None) -> str:
    """统一决策出口（Composer 归一 → 决策矩阵 → 回写）+ composed/writeback 事件。"""
    from app.models.entities import EscalationLog
    from app.services.audit_service import _decide_and_writeback

    reason = {**composed.evidence, **(extra_reason or {})}
    level = _decide_and_writeback(db, task, profile, findings=composed.findings,
                                  llm_confidence=composed.confidence,
                                  llm_summary=composed.summary,
                                  writeback_adapter=writeback_adapter,
                                  override_level=override_level,
                                  extra_reason=reason)
    db.flush()  # autoflush=False：让刚写出的 escalation 行对本查询可见
    esc = (db.query(EscalationLog).filter_by(task_id=task.id)
           .order_by(EscalationLog.id.desc()).first())
    rec.emit(_ev.EVENT_COMPOSED, level=level, terminal=terminal,
             n_findings=len(composed.findings), confidence=composed.confidence,
             degraded=composed.degraded, reused_frames=composed.reused_frames,
             override=override_level is not None)
    rec.emit(_ev.EVENT_WRITEBACK_APPLIED, level=level,
             writeback_status=esc.writeback_status if esc else "none",
             action=esc.action if esc else "",
             target_node=esc.target_node if esc else "")
    logger.info("kernel run %s decided: level=%s frames=%d findings=%d "
                "terminal=%s conf=%s reused=%d",
                task.id, level, len(composed.executed_frames),
                len(composed.findings), terminal, composed.confidence,
                len(composed.reused_frames))
    return level


# ---------------------------------------------------------------------------
# 主入口：run（首轮执行）
# ---------------------------------------------------------------------------

def run_kernel_pipeline(db: Session, task: AuditTask, *, profile: FlowProfile,
                        rules: list, engine: RuleEngine, scope: dict,
                        llm_chain=None, writeback_adapter=None) -> str:
    """内核路径：Planner 展开 → Harness 逐帧执行 → 运行时条件二次展开 → 决策回写。

    返回 "decided"（决策已落库）或 "awaiting_human"（挂起，等待 resume）。
    异常由 run_audit_task 外层捕获置 failed。
    """
    from app.config import settings as _s

    chain = _build_chain(llm_chain, _s)
    rule_findings = _eval_rules(engine, rules, scope)
    excerpts = _prefetch_excerpts(db, task)
    ctx = _build_ctx(db, task, profile, scope=scope, chain=chain,
                     rule_findings=rule_findings, excerpts=excerpts)

    rec = EventRecorder()
    harness = HarnessAgent(default_registry(), on_event=rec.emit)
    planner = AuditPlanner()
    sop = _load_sop(profile)
    rag_query = _rag_query_for(task)
    sink = _RunSink()
    run_frames = _make_frame_runner(db, task, ctx, harness, rec, sink,
                                    rag_query=rag_query, excerpts=excerpts)

    def emit_planned(frames) -> None:
        for f in frames:
            rec.emit(_ev.EVENT_FRAME_PLANNED, frame_id=f.frame_id, kind=f.kind,
                     node_id=f.node_id,
                     required_capabilities=list(f.requirement.required_capabilities),
                     optional_capabilities=list(f.requirement.optional_capabilities))

    try:
        result = planner.plan(sop, task.id, scope)
        emit_planned(result.frames)
        state = run_frames(result.frames)

        # ---- 运行时条件：解析 → 二次展开（内置 SOP 会在此走完 policy/risk/writeback） ----
        state = _drain_continuations(sop, planner, task.id, engine, scope,
                                     sink, run_frames, rec, result, state)

        if state == "awaiting_human" and sink.awaiting_frame is not None:
            if _try_suspend(db, task, sink, rec, ctx):
                return "awaiting_human"
            # 超过挂起轮次上限：强制出决策（T3 兜底语义，矩阵照常 gate）

        _persist_findings(db, task, sink.findings)
        _persist_chain_traces(db, task, ctx.cache_get("llm_chain_result"))
        _decide_and_emit(db, task, profile, rec,
                         composed=_compose(sink, result.terminal),
                         terminal=result.terminal,
                         writeback_adapter=writeback_adapter)
        return "decided"
    finally:
        # 事件流旁路落库：即使主流程异常也不吞异常、不阻断外层 failed 处理
        try:
            rec.flush(db, task.id)
        except Exception:  # noqa: BLE001
            logger.warning("run %s run_event 落库失败", task.id, exc_info=True)


def _rag_query_for(task: AuditTask) -> str:
    from app.services.audit_service import _rag_query
    return _rag_query(task.snapshot or {})


# ---------------------------------------------------------------------------
# 恢复入口：resume（挂起-恢复 + 增量重审，T5）
# ---------------------------------------------------------------------------

def resume_kernel_run(db: Session, task: AuditTask, human_task: HumanTask, *,
                      action: str, operator: str = "", payload: dict | None = None,
                      llm_chain=None, writeback_adapter=None) -> dict:
    """幂等恢复挂起的 run（v2.1 §1.3 + v3.0 §6）。

    - COMMENT：全部已完成帧复用（reused），从 gate 节点续跑下游后决策；
    - SUPPLY_MATERIAL：patch_fields 合并进 snapshot，规则层全量重算，从 entry
      重新展开；帧指纹未变者直接复用上轮结果（不重复执行能力/调用 LLM/
      重复落 findings），变化帧重跑（材料类重跑，摘录未变帧标 reused）；
    - OVERRIDE_PASS / OVERRIDE_REJECT：不经流水线直写终审
      （matrix_reason.cell=human_override, confidence=None），仍走统一回写出口。

    返回 {"resumed": bool, "deduplicated": bool, "level": str|None}。
    不负责 commit（调用方统一提交）。
    """
    from app.config import settings as _s
    from app.services.audit_service import _rag_query, build_scope

    if action not in RESUME_ACTIONS:
        raise ValueError(f"非法人工动作: {action!r}（允许: {list(RESUME_ACTIONS)}）")
    if human_task.status != "pending" or task.status != "awaiting_human":
        return {"resumed": False, "deduplicated": True, "level": None,
                "status": human_task.status}

    profile = db.get(FlowProfile, task.flow_code)
    if profile is None:
        profile = FlowProfile(flow_code=task.flow_code, mode="shadow")
        db.add(profile)
        db.flush()

    # ---- SUPPLY_MATERIAL：patch 合并进 snapshot（决议留痕于 human_task.result） ----
    if action == "SUPPLY_MATERIAL":
        patch = (payload or {}).get("patch_fields") or {}
        if patch:
            snap = dict(task.snapshot or {})
            form = dict(snap.get("form") or {})
            form.update(patch)
            snap["form"] = form
            task.snapshot = snap

    engine = RuleEngine(_make_default_functions())
    scope = build_scope(task.snapshot or {})
    rules = _load_rules(db, task.flow_code)

    # ---- 人工决议留痕 + 幂等标记 ----
    human_task.status = "done"
    human_task.result = {"action": action, "operator": operator,
                         "payload": payload or {}}
    human_task.finished_at = utcnow()

    # ---- 事件续写（seq 从挂起点继续，保持 (run_id, seq) 唯一） ----
    from app.models.entities import RunEvent
    from sqlalchemy import func
    max_seq = db.query(func.max(RunEvent.seq)).filter_by(run_id=task.id).scalar() or 0
    rec = EventRecorder(start_seq=max_seq)
    rec.emit(_ev.EVENT_RESUMED, action=action, operator=operator,
             resume_token=human_task.resume_token, node_id=human_task.node_id)
    harness = HarnessAgent(default_registry(), on_event=rec.emit)
    planner = AuditPlanner()
    sop = _load_sop(profile)

    # ---- 规则层全量重算（毫秒级）+ 上下文重建 ----
    rule_findings = _eval_rules(engine, rules, scope)
    excerpts = _prefetch_excerpts(db, task)
    chain = _build_chain(llm_chain, _s)
    ctx = _build_ctx(db, task, profile, scope=scope, chain=chain,
                     rule_findings=rule_findings, excerpts=excerpts)
    rag_query = _rag_query(task.snapshot or {})
    sink = _RunSink()

    restored_chain = None
    # 链结果还原：COMMENT/OVERRIDE 下游 LLM 帧零重复调用；
    # SUPPLY 材料类帧必须重跑链（快照已变），不还原（run_cache 仅为挂起点快照）。
    if action != "SUPPLY_MATERIAL" and human_task.run_cache \
            and human_task.run_cache.get("llm_chain_result"):
        restored_chain = _restore_chain_cache(ctx, human_task.run_cache)

    # ---- 已完成帧恢复 + reused 判定（指纹按当前输入重算比对） ----
    stored = (db.query(TaskFrameRecord).filter_by(run_id=task.id)
              .order_by(TaskFrameRecord.seq).all())
    for row in stored:
        sink.seq_floor = max(sink.seq_floor, row.seq + 1)
        if row.status != "completed" or not row.outcome:
            continue
        fp = _frame_fingerprint(row.kind, row.node_id, scope=scope,
                                snapshot=task.snapshot or {},
                                rule_findings=rule_findings, excerpts=excerpts,
                                rag_query=rag_query,
                                writeback_tier=profile.writeback_tier)
        reuse = (action == "COMMENT"
                 or (action == "SUPPLY_MATERIAL"
                     and row.outcome.get("fingerprint") == fp))
        if not reuse:
            continue
        oc = FrameOutcome.from_dict(row.outcome)
        oc.frame_id = row.frame_id
        sink.outcomes.append(oc)
        sink.executed.append(row.frame_id)
        sink.reused.append(row.frame_id)
        sink.skip.add((row.node_id, row.kind))
        row.outcome = {**row.outcome, "reused": True}
        rec.emit(_ev.EVENT_FRAME_FINISHED, frame_id=row.frame_id, reused=True,
                 status=oc.status, n_findings=len(oc.findings))

    run_frames = _make_frame_runner(db, task, ctx, harness, rec, sink,
                                    rag_query=rag_query, excerpts=excerpts)

    def emit_planned(frames) -> None:
        for f in frames:
            rec.emit(_ev.EVENT_FRAME_PLANNED, frame_id=f.frame_id, kind=f.kind,
                     node_id=f.node_id,
                     required_capabilities=list(f.requirement.required_capabilities),
                     optional_capabilities=list(f.requirement.optional_capabilities))

    try:
        if action in OVERRIDE_LEVELS:
            # ---- 人工终审：不经流水线直写（矩阵之上的权威，全程留痕） ----
            composed = ComposedDecision(findings=list(rule_findings),
                                        terminal="__override__")
            level = _decide_and_emit(
                db, task, profile, rec, composed=composed,
                terminal="__override__", writeback_adapter=writeback_adapter,
                override_level=OVERRIDE_LEVELS[action],
                extra_reason={"operator": operator})
            _persist_findings(db, task, rule_findings)
            _persist_chain_traces(db, task, ctx.cache_get("llm_chain_result"))
            return {"resumed": True, "deduplicated": False, "level": level}

        if action == "COMMENT":
            # ---- 从 gate 节点续跑下游（全部已完成帧已复用） ----
            nxt = _resolve_next(sop, human_task.node_id,
                                _run_state(sink.findings), engine, scope)
            result = None
            state = "ok"
            if nxt in RESERVED or nxt is None:
                if nxt in RESERVED:
                    logger.info("run %s 恢复后直达终态 %s", task.id, nxt)
            else:
                result = planner.expand(sop, task.id, nxt,
                                        seq_start=sink.seq_floor,
                                        depends_on=[human_task.frame_id],
                                        scope=scope)
                emit_planned(result.frames)
                state = run_frames(result.frames)
                state = _drain_continuations(sop, planner, task.id, engine, scope,
                                             sink, run_frames, rec, result, state)
            terminal = (result.terminal if result is not None
                        else (nxt if nxt in RESERVED else None))
        else:
            # ---- SUPPLY_MATERIAL：从 entry 重新展开（seq 偏移），指纹未变帧跳过 ----
            result = planner.plan(sop, task.id, scope, seq_start=sink.seq_floor)
            emit_planned(result.frames)
            state = run_frames(result.frames)
            state = _drain_continuations(sop, planner, task.id, engine, scope,
                                         sink, run_frames, rec, result, state)
            terminal = result.terminal

        # ---- 恢复后再次挂起（round+1，受轮次上限约束） ----
        if state == "awaiting_human" and sink.awaiting_frame is not None:
            if _try_suspend(db, task, sink, rec, ctx):
                return {"resumed": True, "deduplicated": False, "level": None,
                        "status": "awaiting_human"}
            # 超限：强制出决策

        _persist_findings(db, task, sink.findings)
        _persist_chain_traces(db, task, ctx.cache_get("llm_chain_result"))
        level = _decide_and_emit(
            db, task, profile, rec, composed=_compose(sink, terminal),
            terminal=terminal, writeback_adapter=writeback_adapter,
            extra_reason={"resume_action": action})
        return {"resumed": True, "deduplicated": False, "level": level}
    finally:
        try:
            rec.flush(db, task.id)
        except Exception:  # noqa: BLE001
            logger.warning("run %s run_event 落库失败（resume）", task.id, exc_info=True)


def _restore_chain_cache(ctx: CapabilityContext, run_cache: dict):
    """链结果缓存还原（T5）：恢复后下游 LLM 帧零重复调用。

    traces 已在挂起时点持久化，还原对象带 _traces_persisted 标记防重写。
    """
    from worker.pipeline.llm.chain import LLMChainResult

    obj = LLMChainResult.from_dict(run_cache["llm_chain_result"])
    if run_cache.get("traces_persisted"):
        obj._traces_persisted = True
    ctx.cache_set("llm_chain_result", obj)
    return obj


def _load_rules(db: Session, flow_code: str) -> list:
    from app.models.entities import PolicyRule
    return (db.query(PolicyRule)
            .filter(PolicyRule.flow_code.in_([flow_code, "*"]),
                    PolicyRule.enabled.is_(True))
            .all())


def _make_default_functions():
    from worker.pipeline.rules.functions import make_default_functions
    return make_default_functions()
