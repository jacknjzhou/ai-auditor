"""Harness Agent —— 每帧一个隔离执行循环（v3.0 内核设计 §4.3）。

对标 StaffDeck harness_agent：
- 串行动作协议：每轮至多一个 tool，拿到结果后再决策；`action: tool | finish`；
- 协议修复：actor 输出非法 JSON / 形状错误时回注 protocol_repair 提示重试，
  超限则 finish failed（protocol_error）；
- 能力白名单：tool_name 必须已注册且在当前帧 allowed（required+optional）内；
- 预算硬拦截：budgeted 能力（knowledge_search）超限返回 budget_exhausted
  （非致命，retryable=false），actor 必须基于已有证据收尾；
- 重试纪律：retryable=false 的失败禁止同签名重试（对齐 StaffDeck 错误协议）；
- 完成门槛：finish completed 前 required_capabilities 必须全部成功执行，
  否则进入协议修复；修复后仍不满足 → finish failed（missing_required_capabilities）。

与 StaffDeck 的差异：actor 的"自主"被限制在当前帧白名单内；帧边界与 SOP 推进
由 Planner/状态机持有，Harness 不得重新路由（next_step 概念不存在于此层）。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from app.schemas.canonical import FindingOut
from worker.pipeline.kernel.capabilities import (
    CapabilityCall,
    CapabilityContext,
    CapabilityRegistry,
    digest,
)
from worker.pipeline.kernel.frames import TaskFrame

logger = logging.getLogger(__name__)

VALID_FINISH_STATUS = {"completed", "awaiting_human", "failed"}
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


class ProtocolError(Exception):
    """actor 输出违反动作协议（触发修复重试）。"""

    def __init__(self, message: str, *, repair_hint: str = ""):
        super().__init__(message)
        self.repair_hint = repair_hint or message


# ---------------------------------------------------------------------------
# actor 协议
# ---------------------------------------------------------------------------
# actor: Callable[[list[dict]], str | dict]
#   入参 turns：本帧隔离 transcript（requirement/capability_result/repair 观察序列）
#   出参：动作 JSON（str 原始文本或已解析 dict，均可）

ActorFn = Callable[[list[dict[str, str]]], Any]


def scripted_actor(actions: list[Any]) -> ActorFn:
    """测试/确定性节点用：按序回放动作脚本（耗尽后强制 finish completed）。"""
    queue = list(actions)

    def _actor(turns: list[dict[str, str]]) -> Any:
        return queue.pop(0) if queue else {"action": "finish", "status": "completed"}

    return _actor


# ---------------------------------------------------------------------------
# 结果
# ---------------------------------------------------------------------------

@dataclass
class FrameOutcome:
    """帧执行结果（Composer 的输入；对应 run_event 的 frame_finished 载荷）。"""

    frame_id: str
    status: str = "failed"  # completed | awaiting_human | failed
    findings: list[FindingOut] = field(default_factory=list)
    confidence: float | None = None
    risk_flags: list[str] = field(default_factory=list)
    reply_fragment: str = ""
    structured_result: dict[str, Any] | None = None
    summary: str = ""
    calls: list[CapabilityCall] = field(default_factory=list)
    protocol_repairs: int = 0
    loops: int = 0
    degraded: bool = False
    halt_run: bool = False  # failure_policy=halt 且失败时置 True（Planner/Composer 消费）

    def ok_calls(self) -> list[CapabilityCall]:
        return [c for c in self.calls if c.ok]

    def executed(self) -> list[str]:
        return [c.name for c in self.ok_calls()]

    def missing_required(self, requirement: Any) -> list[str]:
        done = set(self.executed())
        return [c for c in requirement.required_capabilities if c not in done]

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame_id": self.frame_id, "status": self.status,
            "confidence": self.confidence, "risk_flags": self.risk_flags,
            "reply_fragment": self.reply_fragment,
            "structured_result": self.structured_result,
            "summary": self.summary, "degraded": self.degraded,
            "halt_run": self.halt_run,
            "findings": [f.model_dump() for f in self.findings],
            "calls": [c.to_dict() for c in self.calls],
            "protocol_repairs": self.protocol_repairs, "loops": self.loops,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FrameOutcome":
        """从 task_frame.outcome JSON 恢复（T5 增量重审：指纹未变帧直接复用）。

        calls 不恢复执行记录（复用帧不再执行能力，不产生新调用留痕）。
        """
        return cls(
            frame_id=str(d.get("frame_id", "")),
            status=str(d.get("status", "completed")),
            findings=[FindingOut.model_validate(f) for f in d.get("findings") or []],
            confidence=d.get("confidence"),
            risk_flags=list(d.get("risk_flags") or []),
            reply_fragment=str(d.get("reply_fragment", "")),
            structured_result=d.get("structured_result"),
            summary=str(d.get("summary", "")),
            degraded=bool(d.get("degraded")),
            halt_run=bool(d.get("halt_run")),
        )


def _collect_findings(calls: list[CapabilityCall],
                      results: dict[int, dict]) -> list[FindingOut]:
    """从能力结果中收集 findings（dict → FindingOut，非法条目丢弃并告警）。"""
    out: list[FindingOut] = []
    for call in calls:
        result = results.get(id(call))
        if not (call.ok and result):
            continue
        for raw in result.get("findings") or []:
            try:
                out.append(FindingOut.model_validate(raw))
            except Exception:  # noqa: BLE001 —— 白名单外结构直接丢弃
                logger.warning("丢弃非法 finding: %r", raw)
    return out


# ---------------------------------------------------------------------------
# Harness Agent
# ---------------------------------------------------------------------------

class HarnessAgent:
    """串行工具循环执行器。每帧独立实例或复用均可（无跨帧状态）。"""

    def __init__(
        self,
        registry: CapabilityRegistry,
        *,
        max_loops: int = 8,
        repair_attempts: int = 2,
        criteria_evaluator: Callable[[list[str], FrameOutcome, TaskFrame],
                                     list[str]] | None = None,
        on_event: Callable[..., Any] | None = None,
    ) -> None:
        self.registry = registry
        self.max_loops = max_loops
        self.repair_attempts = repair_attempts
        # completion_criteria 校验钩子：返回未满足项清单；缺省视为全部满足
        self.criteria_evaluator = criteria_evaluator or (lambda criteria, o, f: [])
        # T4 埋点钩子：emit(event_type, frame_id=..., **payload)；埋点失败不影响执行
        self.on_event = on_event

    def _emit(self, event_type: str, frame_id: str, **payload: Any) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(event_type, frame_id=frame_id, **payload)
        except Exception:  # noqa: BLE001 —— 埋点属于旁路观测，绝不打断主流程
            logger.warning("run_event 埋点失败 (%s/%s)", event_type, frame_id,
                           exc_info=True)

    # ---------- 动作解析 ----------
    @staticmethod
    def _parse_action(raw: Any) -> dict[str, Any]:
        """解析 actor 输出为动作 dict；非法抛 ProtocolError。"""
        if isinstance(raw, dict):
            action = raw
        elif isinstance(raw, str):
            text = raw.strip()
            fence = _FENCE_RE.search(text)
            if fence:
                text = fence.group(1)
            try:
                action = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ProtocolError(f"输出不是合法 JSON: {exc}") from exc
        else:
            raise ProtocolError(f"动作类型非法: {type(raw).__name__}")

        if not isinstance(action, dict) or "action" not in action:
            raise ProtocolError("缺少 action 字段", repair_hint="输出形如 "
                '{"action":"tool","tool_name":"...","arguments":{}} 或 '
                '{"action":"finish","status":"completed",...}')

        kind = action.get("action")
        if kind == "tool":
            tool_name = action.get("tool_name")
            if not isinstance(tool_name, str) or not tool_name:
                raise ProtocolError("tool 动作缺少 tool_name")
            if "arguments" in action and not isinstance(action["arguments"], dict):
                raise ProtocolError("arguments 必须是对象")
            return action
        if kind == "finish":
            status = action.get("status")
            if status not in VALID_FINISH_STATUS:
                raise ProtocolError(
                    f"finish.status 非法: {status!r}（只允许 {sorted(VALID_FINISH_STATUS)}）")
            return action
        raise ProtocolError(
            f"未知 action: {kind!r}", repair_hint="action 只能是 tool 或 finish")

    # ---------- 主循环 ----------
    def run(self, frame: TaskFrame, ctx: CapabilityContext,
            actor: ActorFn) -> FrameOutcome:
        req = frame.requirement
        outcome = FrameOutcome(frame_id=frame.frame_id)
        results_by_call: dict[int, dict] = {}
        turns: list[dict[str, str]] = [{
            "role": "requirement",
            "content": json.dumps({
                "frame": {"kind": frame.kind, "node_id": frame.node_id},
                "requirement": req.to_dict(),
                "capability_catalog": self.registry.catalog(
                    sorted(req.allowed_capabilities())),
            }, ensure_ascii=False),
        }]
        no_retry: set[str] = set()
        budget_used = 0

        for loop in range(1, self.max_loops + 1):
            outcome.loops = loop
            raw = actor(turns)

            # ---- 解析 + 协议修复 ----
            try:
                action = self._parse_action(raw)
            except ProtocolError as exc:
                if outcome.protocol_repairs >= self.repair_attempts:
                    return self._failed(outcome, frame, "protocol_error",
                                        f"协议修复超限: {exc}")
                outcome.protocol_repairs += 1
                self._emit("protocol_repair", frame.frame_id,
                           reason=str(exc), hint=exc.repair_hint,
                           repairs=outcome.protocol_repairs)
                turns.append({"role": "repair",
                              "content": f"protocol_repair: {exc.repair_hint}"})
                continue

            kind = action["action"]

            # ---- finish ----
            if kind == "finish":
                status = action["status"]
                missing = outcome.missing_required(req)
                if status == "completed" and missing:
                    hint = (f"completed 前必须成功执行全部强制能力，"
                            f"缺失: {missing}")
                    if outcome.protocol_repairs >= self.repair_attempts:
                        return self._failed(outcome, frame,
                                            "missing_required_capabilities", hint)
                    outcome.protocol_repairs += 1
                    self._emit("protocol_repair", frame.frame_id,
                               reason="missing_required_capabilities",
                               hint=hint, repairs=outcome.protocol_repairs)
                    turns.append({"role": "repair",
                                  "content": f"protocol_repair: {hint}"})
                    continue

                unmet = self.criteria_evaluator(
                    req.completion_criteria, outcome, frame)
                if status == "completed" and unmet:
                    hint = f"完成标准未满足: {unmet}"
                    if outcome.protocol_repairs >= self.repair_attempts:
                        return self._failed(outcome, frame, "criteria_unmet", hint)
                    outcome.protocol_repairs += 1
                    self._emit("protocol_repair", frame.frame_id,
                               reason="criteria_unmet", hint=hint,
                               repairs=outcome.protocol_repairs)
                    turns.append({"role": "repair",
                                  "content": f"protocol_repair: {hint}"})
                    continue

                return self._finish(outcome, frame, action, results_by_call)

            # ---- tool ----
            tool_name: str = action["tool_name"]
            arguments: dict = action.get("arguments") or {}

            if not self.registry.has(tool_name):
                hint = f"能力未注册: {tool_name}，可用: {self.registry.names()}"
                if outcome.protocol_repairs >= self.repair_attempts:
                    return self._failed(outcome, frame, "protocol_error", hint)
                outcome.protocol_repairs += 1
                self._emit("protocol_repair", frame.frame_id,
                           reason="unknown_capability", hint=hint,
                           repairs=outcome.protocol_repairs)
                turns.append({"role": "repair", "content": f"protocol_repair: {hint}"})
                continue

            if tool_name not in req.allowed_capabilities():
                hint = (f"能力未授权: {tool_name}（本帧白名单: "
                        f"{sorted(req.allowed_capabilities())}）——审核场景禁止越界调用")
                if outcome.protocol_repairs >= self.repair_attempts:
                    return self._failed(outcome, frame, "capability_forbidden", hint)
                outcome.protocol_repairs += 1
                self._emit("protocol_repair", frame.frame_id,
                           reason="capability_forbidden", hint=hint,
                           repairs=outcome.protocol_repairs)
                turns.append({"role": "repair", "content": f"protocol_repair: {hint}"})
                continue

            spec = self.registry.spec(tool_name)

            # 预算硬拦截（非致命）：actor 须基于已有证据收尾
            if spec.budgeted and budget_used >= req.knowledge_budget:
                record = CapabilityCall(
                    name=tool_name, arguments_digest=digest(arguments),
                    ok=False, error="budget_exhausted", retryable=False,
                    budget_consumed=False)
                outcome.calls.append(record)
                self._emit("capability_called", frame.frame_id,
                           tool=tool_name, arguments_digest=record.arguments_digest)
                self._emit("capability_result", frame.frame_id, tool=tool_name,
                           ok=False, error="budget_exhausted", retryable=False,
                           latency_ms=0, budget_left=0)
                turns.append({"role": "observation", "content": json.dumps(
                    {"tool": tool_name, "ok": False, "error": "budget_exhausted",
                     "retryable": False,
                     "note": f"知识预算({req.knowledge_budget})已耗尽，请基于已有证据 finish"},
                    ensure_ascii=False)})
                continue

            # retryable=false 的失败禁止同签名重试（签名 = 能力名 + 确定性入参摘要）
            probe = CapabilityCall(name=tool_name, arguments_digest=digest(arguments))
            if probe.signature() in no_retry:
                hint = (f"同签名调用此前已失败且 retryable=false，禁止原样重试: {tool_name}；"
                        "请更换工具/参数或 finish 说明失败")
                if outcome.protocol_repairs >= self.repair_attempts:
                    return self._failed(outcome, frame, "retry_forbidden", hint)
                outcome.protocol_repairs += 1
                self._emit("protocol_repair", frame.frame_id,
                           reason="retry_forbidden", hint=hint,
                           repairs=outcome.protocol_repairs)
                turns.append({"role": "repair", "content": f"protocol_repair: {hint}"})
                continue

            self._emit("capability_called", frame.frame_id, tool=tool_name,
                       arguments_digest=probe.arguments_digest)
            record, result = self.registry.call(tool_name, ctx, arguments)
            outcome.calls.append(record)
            if spec.budgeted and record.ok:
                budget_used += 1
                record.budget_consumed = True
            budget_left = (req.knowledge_budget - budget_used
                           if spec.budgeted else None)
            self._emit("capability_result", frame.frame_id, tool=tool_name,
                       ok=record.ok, error=record.error, retryable=record.retryable,
                       latency_ms=record.latency_ms, budget_left=budget_left)

            if not record.ok:
                if not record.retryable:
                    no_retry.add(record.signature())
                observation = {"tool": tool_name, "ok": False,
                               "error": record.error, "retryable": record.retryable}
            else:
                results_by_call[id(record)] = result or {}
                observation = {"tool": tool_name, "ok": True, "result": result}
            turns.append({"role": "observation",
                          "content": json.dumps(observation, ensure_ascii=False,
                                                default=str)[:4000]})

        return self._failed(outcome, frame, "loop_limit",
                            f"超过最大循环次数 {self.max_loops}")

    # ---------- 收尾 ----------
    @staticmethod
    def _apply_finish(outcome: FrameOutcome, frame: TaskFrame,
                      action: dict[str, Any],
                      results_by_call: dict[int, dict]) -> None:
        outcome.status = action["status"]
        outcome.reply_fragment = str(action.get("reply_fragment", ""))
        outcome.summary = str(action.get("task_summary", ""))
        sr = action.get("structured_result")
        outcome.structured_result = sr if isinstance(sr, dict) else None

        outcome.findings = _collect_findings(outcome.calls, results_by_call)
        # structured_result 中的置信度/风险标记并入帧结果（llm_assess 能力约定）
        if sr and isinstance(sr.get("confidence"), (int, float)):
            outcome.confidence = float(sr["confidence"])
        if sr and isinstance(sr.get("risk_flags"), list):
            outcome.risk_flags = [str(x) for x in sr["risk_flags"]]

        # 帧降级判定：强制能力存在失败/预算耗尽记录
        failed_required = [c for c in outcome.calls
                           if not c.ok and c.name in frame.requirement.required_capabilities]
        outcome.degraded = bool(failed_required)

    def _finish(self, outcome: FrameOutcome, frame: TaskFrame,
                action: dict[str, Any],
                results_by_call: dict[int, dict]) -> FrameOutcome:
        self._apply_finish(outcome, frame, action, results_by_call)
        status = outcome.status
        if status == "failed":
            outcome.halt_run = frame.requirement.failure_policy == "halt"
        if status == "awaiting_human":
            self._emit("awaiting_human", frame.frame_id, summary=outcome.summary)
        # 失败能力 + degrade 策略：帧标记 degraded 但保持状态（Composer 归一化）
        logger.info("frame %s finished: status=%s loops=%d calls=%d repairs=%d",
                    frame.frame_id, status, outcome.loops, len(outcome.calls),
                    outcome.protocol_repairs)
        return outcome

    def _failed(self, outcome: FrameOutcome, frame: TaskFrame,
                reason: str, detail: str) -> FrameOutcome:
        outcome.status = "failed"
        outcome.summary = f"{reason}: {detail}"
        outcome.degraded = True
        outcome.halt_run = frame.requirement.failure_policy == "halt"
        outcome.structured_result = {"failure_reason": reason}
        logger.warning("frame %s failed (%s): %s", frame.frame_id, reason, detail)
        return outcome
