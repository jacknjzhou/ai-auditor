"""EventRecorder —— run_event 全链路埋点收集器（v3.0 内核设计 §6，T4）。

设计要点：
- **内存累积、统一落库**：run 执行期内零 DB 往返（埋点不打断热路径），
  flush 时一次性写入 run_event + capability_call，由调用方 commit；
- **seq 单调递增**：(run_id, seq) 唯一，事件顺序即执行顺序，增量查询
  （after_seq）以此为游标；
- **事件类型白名单**（设计 §6 九类）：frame_planned / frame_started /
  capability_called / capability_result / protocol_repair / awaiting_human /
  frame_finished / composed / writeback_applied；
- 埋点失败绝不影响审核主流程：emit/record 全程纯内存，flush 异常仅告警。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from worker.pipeline.kernel.capabilities import CapabilityCall

logger = logging.getLogger(__name__)

EVENT_FRAME_PLANNED = "frame_planned"
EVENT_FRAME_STARTED = "frame_started"
EVENT_CAPABILITY_CALLED = "capability_called"
EVENT_CAPABILITY_RESULT = "capability_result"
EVENT_PROTOCOL_REPAIR = "protocol_repair"
EVENT_AWAITING_HUMAN = "awaiting_human"
EVENT_FRAME_FINISHED = "frame_finished"
EVENT_COMPOSED = "composed"
EVENT_WRITEBACK_APPLIED = "writeback_applied"
EVENT_RESUMED = "resumed"  # v2.1 §3：挂起-恢复事件（T5）

EVENT_TYPES = frozenset({
    EVENT_FRAME_PLANNED, EVENT_FRAME_STARTED, EVENT_CAPABILITY_CALLED,
    EVENT_CAPABILITY_RESULT, EVENT_PROTOCOL_REPAIR, EVENT_AWAITING_HUMAN,
    EVENT_FRAME_FINISHED, EVENT_COMPOSED, EVENT_WRITEBACK_APPLIED,
    EVENT_RESUMED,
})


@dataclass
class _PendingEvent:
    seq: int
    event_type: str
    frame_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class _PendingCapabilityCall:
    frame_id: str
    call: CapabilityCall
    budget_left: int | None = None


class EventRecorder:
    """单次 run 的事件收集器（无跨 run 状态；T5 挂起-恢复按 run_id 续写）。"""

    def __init__(self, start_seq: int = 0) -> None:
        # start_seq：run_event 已有最大 seq（挂起-恢复场景续写，保持 (run_id,seq) 唯一）
        self._seq = start_seq
        self._events: list[_PendingEvent] = []
        self._cap_calls: list[_PendingCapabilityCall] = []
        self._flushed = False

    # ---------- 采集 ----------
    def emit(self, event_type: str, *, frame_id: str = "",
             **payload: Any) -> int:
        """记录一条事件，返回其 seq（未知类型拒绝——事件流是审计契约）。"""
        if event_type not in EVENT_TYPES:
            raise ValueError(f"未知事件类型: {event_type!r}（允许: {sorted(EVENT_TYPES)}）")
        self._seq += 1
        self._events.append(_PendingEvent(seq=self._seq, event_type=event_type,
                                          frame_id=frame_id, payload=payload))
        return self._seq

    def record_capability(self, frame_id: str, call: CapabilityCall,
                          budget_left: int | None = None) -> None:
        """记录一次能力调用（写入 capability_call 表；同 seq 事件流镜像）。"""
        self._cap_calls.append(_PendingCapabilityCall(frame_id=frame_id,
                                                      call=call,
                                                      budget_left=budget_left))

    # ---------- 观测（测试/调试用） ----------
    @property
    def current_seq(self) -> int:
        """当前最大 seq（挂起时写入 human_task.seq 的取值来源）。"""
        return self._seq

    def snapshot(self) -> list[tuple[int, str, str]]:
        """返回 (seq, event_type, frame_id) 紧凑序列，供快照比对。"""
        return [(e.seq, e.event_type, e.frame_id) for e in self._events]

    # ---------- 落库 ----------
    def flush(self, db: Any, run_id: str) -> int:
        """写入 run_event + capability_call；幂等（重复 flush 为 no-op）。"""
        if self._flushed:
            return 0
        from app.models.entities import CapabilityCallRecord, RunEvent

        for e in self._events:
            db.add(RunEvent(run_id=run_id, seq=e.seq, event_type=e.event_type,
                            frame_id=e.frame_id, payload=e.payload))
        for c in self._cap_calls:
            db.add(CapabilityCallRecord(
                run_id=run_id, frame_id=c.frame_id, capability=c.call.name,
                arguments_digest=c.call.arguments_digest,
                result_digest=c.call.result_digest, ok=c.call.ok,
                retryable=c.call.retryable, latency_ms=c.call.latency_ms,
                budget_left=c.budget_left, error=c.call.error[:1000]))
        n = len(self._events)
        self._flushed = True
        logger.info("run %s 事件落库: %d events, %d capability calls",
                    run_id, n, len(self._cap_calls))
        return n
