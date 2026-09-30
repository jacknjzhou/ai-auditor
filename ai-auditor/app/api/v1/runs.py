"""Run 事件流查询 + 挂起-恢复 API —— v3.0 内核设计 §6 / v2.1 §1.3（T4/T5）。

增量查询先行（SSE 留 P4）：
- GET  /api/v1/runs/{run_id}/events?after_seq=N      事件流增量拉取（游标=after_seq）；
- GET  /api/v1/runs/{run_id}/capability-calls         能力调用留痕（预算/延迟审计）；
- POST /api/v1/audit-tasks/{run_id}/resume            人工恢复（HMAC 验签同 webhook；
                                                       resume_token 幂等，重复回调不二次恢复）。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.models.entities import (AuditTask, CapabilityCallRecord, HumanTask,
                                 RunEvent, get_db)
from app.services.kernel_runner import RESUME_ACTIONS, resume_kernel_run

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


@router.post("/audit-tasks/{run_id}/resume")
async def resume_run(run_id: str, request: Request,
                     db: Session = Depends(get_db)) -> dict:
    """人工恢复挂起的 run（v2.1 §1.3：HMAC 验签 + resume_token 幂等）。

    Body: {"action": "SUPPLY_MATERIAL|OVERRIDE_PASS|OVERRIDE_REJECT|COMMENT",
           "resume_token": "...", "operator": "u123",
           "payload": {"patch_fields": {...}, "comment": "..."}}
    """
    from app.api.v1.webhooks import _secret_for, verify_signature

    raw = await request.body()
    verify_signature(raw, request.headers, _secret_for(db, "resume"))
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid json") from exc

    action = body.get("action")
    if action not in RESUME_ACTIONS:
        raise HTTPException(status_code=422,
                            detail=f"action 非法: {action!r}（允许: {list(RESUME_ACTIONS)}）")
    token = str(body.get("resume_token") or "")
    if not token:
        raise HTTPException(status_code=422, detail="resume_token required")

    task = db.get(AuditTask, run_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"run 不存在: {run_id}")
    ht = (db.query(HumanTask)
          .filter_by(run_id=run_id, resume_token=token)
          .order_by(HumanTask.created_at.desc()).first())
    if ht is None:
        raise HTTPException(status_code=404, detail="resume_token 与 run 不匹配")

    out = resume_kernel_run(db, task, ht, action=action,
                            operator=str(body.get("operator") or ""),
                            payload=body.get("payload") or {})
    db.commit()
    return {"run_id": run_id, **out}
