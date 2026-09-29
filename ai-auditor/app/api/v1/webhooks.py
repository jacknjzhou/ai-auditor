"""Webhook 入站 API —— 对应详细设计 §3.2 / §4.1。

安全约定：
- HMAC-SHA256 签名：X-Auditor-Signature: sha256=<hex(hmac(secret, "{ts}.{raw_body}"))>
- 时间戳防重放：±AUDITOR_WEBHOOK_REPLAY_WINDOW_SECONDS
- 幂等：idempotency_key = sha256(source|instance|node|event_type|version)
  重放同一事件返回原 task_id（deduplicated=true），不产生新任务。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import settings
from app.models.entities import (AuditDecision, AuditFinding, ApprovalEvent,
                                 AuditTask, ConnectorConfig, get_db, utcnow)
from app.services.dispatcher import dispatch_audit_task

router = APIRouter()


def _secret_for(db: Session, source_code: str) -> str:
    conn = db.query(ConnectorConfig).filter_by(source_code=source_code).first()
    return (conn.webhook_secret if conn and conn.webhook_secret
            else settings.webhook_secret_default)


def verify_signature(raw: bytes, headers, secret: str) -> None:
    sig = headers.get("x-auditor-signature", "")
    ts = headers.get("x-auditor-timestamp", "")
    if not sig.startswith("sha256=") or not ts:
        raise HTTPException(status_code=401, detail="missing signature headers")
    try:
        ts_val = int(ts)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="bad timestamp") from exc
    if abs(time.time() - ts_val) > settings.webhook_replay_window_seconds:
        raise HTTPException(status_code=401, detail="timestamp replay window exceeded")
    expected = hmac.new(secret.encode(), ts.encode() + b"." + raw,
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig[len("sha256="):]):
        raise HTTPException(status_code=401, detail="signature mismatch")


def _idempotency_key(payload: dict, source_code: str) -> str:
    inst = payload.get("instance", {})
    basis = "|".join([
        source_code,
        str(inst.get("instance_id", "")),
        str(inst.get("current_node", "")),
        str(payload.get("event_type", "")),
        str(payload.get("version", 0)),
    ])
    return hashlib.sha256(basis.encode()).hexdigest()


def _build_snapshot(payload: dict) -> dict:
    inst = payload.get("instance", {})
    return {
        "flow_code": inst.get("flow_code", ""),
        "node_code": inst.get("current_node", ""),
        "form": inst.get("form", {}),
        "attachments": inst.get("attachments", []),
        "applicant": inst.get("applicant", {}),
        "submitted_at": inst.get("submitted_at"),
        "ctx": inst.get("ctx", {}),
        "std": inst.get("std", {}),
    }


@router.post("/webhooks/{source_code}", status_code=202)
async def receive_event(source_code: str, request: Request,
                        background: BackgroundTasks,
                        db: Session = Depends(get_db)) -> dict:
    raw = await request.body()
    verify_signature(raw, request.headers, _secret_for(db, source_code))

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid json") from exc

    inst = payload.get("instance", {})
    if not inst.get("instance_id"):
        raise HTTPException(status_code=422, detail="instance.instance_id required")

    key = _idempotency_key(payload, source_code)
    existing = db.query(ApprovalEvent).filter_by(idempotency_key=key).first()
    if existing is not None:
        task = (db.query(AuditTask).filter_by(event_id=existing.id)
                .order_by(AuditTask.created_at.desc()).first())
        return {"task_id": task.id if task else None,
                "deduplicated": True, "status": "accepted"}

    event = ApprovalEvent(source_code=source_code,
                          instance_id=inst.get("instance_id", ""),
                          node_code=inst.get("current_node", ""),
                          event_type=payload.get("event_type", ""),
                          idempotency_key=key,
                          version_no=int(payload.get("version", 0)),
                          raw_payload=payload)
    db.add(event)
    db.flush()

    task = AuditTask(event_id=event.id, source_code=source_code,
                     instance_id=inst.get("instance_id", ""),
                     node_code=inst.get("current_node", ""),
                     flow_code=inst.get("flow_code", ""),
                     snapshot=_build_snapshot(payload),
                     status="received", mode="shadow",
                     created_at=utcnow())
    db.add(task)
    db.commit()

    # P1：按 queue_mode 派发——inline 后台执行或 Arq 队列（Redis 故障自动降级 inline）
    channel = await dispatch_audit_task(task.id, background)
    return {"task_id": task.id, "deduplicated": False,
            "status": "accepted", "dispatch": channel}


@router.get("/audit-tasks/{task_id}")
def get_task(task_id: str, db: Session = Depends(get_db)) -> dict:
    task = db.get(AuditTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    decisions = (db.query(AuditDecision)
                 .filter_by(task_id=task_id)
                 .order_by(AuditDecision.created_at.desc()).all())
    findings = db.query(AuditFinding).filter_by(task_id=task_id).all()
    return {
        "task_id": task.id,
        "instance_id": task.instance_id,
        "flow_code": task.flow_code,
        "status": task.status,
        "findings": [
            {"problem_code": f.problem_code, "severity": f.severity,
             "engine": f.engine, "title": f.title, "rule_code": f.rule_code}
            for f in findings
        ],
        "decision": (
            {"level": decisions[0].level, "confidence": decisions[0].confidence,
             "matrix_reason": decisions[0].matrix_reason}
            if decisions else None
        ),
    }
