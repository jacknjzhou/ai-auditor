"""流程 SOP 管理 API —— 新流零代码接入（v3.0 设计 §9-T6）。

- GET  /api/v1/flows/{flow_code}/sop   读取当前 SOP + 路由表（控制台展示）；
- PUT  /api/v1/flows/{flow_code}/sop   安装/更新 SOP（内联 DSL，装载期静态校验）；
- POST /api/v1/flows/{flow_code}/sop/validate  仅校验不落库（编辑期预检）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.models.entities import FlowProfile, get_db
from worker.pipeline.kernel.sop_registry import install_flow_sop, validate_sop

router = APIRouter()


class SopInstall(BaseModel):
    sop: dict = Field(..., description="SOP 状态机 DSL（节点/转移/能力白名单）")
    route_table: list | None = None
    mode: str = "advisory"
    writeback_tier: str = "COMMENT_ONLY"
    amount_cap: float | None = None
    auto_pass_enabled: bool = False


@router.get("/flows/{flow_code}/sop")
def get_flow_sop(flow_code: str, db: Session = Depends(get_db)) -> dict:
    profile = db.get(FlowProfile, flow_code)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"flow 不存在: {flow_code}")
    return {
        "flow_code": flow_code,
        "mode": profile.mode,
        "writeback_tier": profile.writeback_tier,
        "audit_sop": profile.audit_sop,          # None = 内置等价 SOP
        "is_builtin": profile.audit_sop is None,
        "route_table": profile.route_table or [],
    }


@router.post("/flows/{flow_code}/sop/validate")
def validate_flow_sop(flow_code: str, body: SopInstall) -> dict:
    try:
        validate_sop(body.sop)
    except Exception as exc:  # noqa: BLE001 —— 校验失败以 422 反馈
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    nodes = body.sop.get("nodes") or []
    return {"ok": True, "flow_code": flow_code, "n_nodes": len(nodes),
            "entry": body.sop.get("entry")}


@router.put("/flows/{flow_code}/sop")
def put_flow_sop(flow_code: str, body: SopInstall,
                 db: Session = Depends(get_db)) -> dict:
    try:
        profile = install_flow_sop(
            db, flow_code, sop=body.sop, route_table=body.route_table,
            mode=body.mode, writeback_tier=body.writeback_tier,
            amount_cap=body.amount_cap, auto_pass_enabled=body.auto_pass_enabled)
    except Exception as exc:  # noqa: BLE001 —— 静态校验失败
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    return {"ok": True, "flow_code": flow_code, "mode": profile.mode,
            "n_nodes": len(body.sop.get("nodes") or []),
            "installer": "zero-code"}
