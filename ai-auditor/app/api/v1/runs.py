"""Run 事件流查询 API —— v3.0 内核设计 §6（T4）。

增量查询先行（SSE 留 P4）：
- GET /api/v1/runs/{run_id}/events?after_seq=N  返回 seq > N 的事件（按 seq 升序）；
- GET /api/v1/runs/{run_id}/capability-calls     能力调用留痕（预算/延迟审计）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.models.entities import (AuditTask, CapabilityCallRecord, RunEvent,
                                 get_db)

router = APIRouter()


@router.get("/runs/{run_id}/events")
def list_run_events(run_id: str, after_seq: int = 0, limit: int = Query(200, le=1000),
                    db: Session = Depends(get_db)) -> dict:
    """按 seq 增量拉取 run 事件流（游标 = after_seq，客户端循环拉取直到不足 limit）。"""
    if db.get(AuditTask, run_id) is None:
        raise HTTPException(status_code=404, detail=f"run 不存在: {run_id}")
    q = (db.query(RunEvent)
         .filter(RunEvent.run_id == run_id, RunEvent.seq > after_seq)
         .order_by(RunEvent.seq)
         .limit(limit))
    events = [{
        "seq": e.seq, "event_type": e.event_type, "frame_id": e.frame_id,
        "payload": e.payload, "created_at": e.created_at.isoformat(),
    } for e in q]
    return {"run_id": run_id, "after_seq": after_seq,
            "count": len(events), "events": events}


@router.get("/runs/{run_id}/capability-calls")
def list_capability_calls(run_id: str, db: Session = Depends(get_db)) -> dict:
    """能力调用留痕（预算审计：budget_left / latency_ms / 重试分类）。"""
    if db.get(AuditTask, run_id) is None:
        raise HTTPException(status_code=404, detail=f"run 不存在: {run_id}")
    rows = (db.query(CapabilityCallRecord)
            .filter(CapabilityCallRecord.run_id == run_id)
            .order_by(CapabilityCallRecord.id)
            .all())
    calls = [{
        "frame_id": r.frame_id, "capability": r.capability, "ok": r.ok,
        "retryable": r.retryable, "latency_ms": r.latency_ms,
        "budget_left": r.budget_left,
        "arguments_digest": r.arguments_digest, "result_digest": r.result_digest,
        "error": r.error,
    } for r in rows]
    return {"run_id": run_id, "count": len(calls), "calls": calls}
