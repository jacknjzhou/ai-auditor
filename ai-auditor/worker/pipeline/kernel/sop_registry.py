"""SOP 文件装载 + 流程零代码接入（v3.0 设计 §5 / §9-T6）。

**"新流零代码接入"** 只需两份配置，不必改动 Runner / Planner / Harness 任何代码：

1. SOP 状态机（YAML/JSON）：节点 / 转移 / 能力白名单 / 知识预算 / 失败策略；
2. 路由表（problem_code → 处置动作 / 目标节点）。

本模块把两者写入 `flow_profile`（audit_sop / route_table / mode / writeback_tier），
与 webhook → dispatcher → kernel_runner 的既有链路天然接通（`_load_sop(profile)`
优先读 `profile.audit_sop`，为空则回退内置等价 SOP，保证零迁移）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.models.entities import FlowProfile
from worker.pipeline.kernel.planner import AuditSOP


def load_config(path: str | Path) -> Any:
    """读取 YAML/JSON 配置（.json 走标准库，其余走 yaml）。"""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() == ".json":
        return json.loads(text)
    import yaml  # 延迟导入：仅文件装载路径需要

    return yaml.safe_load(text)


def load_sop(path: str | Path) -> dict:
    """装载并**静态校验** SOP，返回可直接写入 `flow_profile.audit_sop` 的 dict。

    校验项（AuditSOP.sop_validate）：节点 id 唯一、转移目标存在（或保留字）、
    条件形态合法、能力声明非空等——坏配置在装载期即失败，不进运行时。
    """
    dsl = load_config(path)
    if not isinstance(dsl, dict):
        raise ValueError(f"SOP 配置必须是映射对象: {path}")
    AuditSOP(dsl)
    return dsl


def validate_sop(dsl: dict) -> dict:
    """校验内存中的 SOP DSL（管理 API 用）；返回原对象，非法则抛错。"""
    if not isinstance(dsl, dict):
        raise ValueError("SOP 必须是映射对象")
    AuditSOP(dsl)
    return dsl


def install_flow_sop(db: Session, flow_code: str, sop_path: str | Path | None = None,
                     *, sop: dict | None = None,
                     route_table_path: str | Path | None = None,
                     route_table: list | None = None,
                     mode: str = "advisory",
                     writeback_tier: str = "COMMENT_ONLY",
                     amount_cap: float | None = None,
                     auto_pass_enabled: bool = False) -> FlowProfile:
    """零代码接入：把 SOP + 路由表写入 `flow_profile`（幂等 upsert）。

    sop_path / route_table_path 与 sop / route_table 二选一（前者为文件装载，
    后者为管理 API 内联）。返回 upsert 后的 FlowProfile（调用方负责 commit）。
    """
    if sop is None:
        if sop_path is None:
            raise ValueError("必须提供 sop 或 sop_path")
        dsl = load_sop(sop_path)
    else:
        dsl = validate_sop(sop)

    routes = route_table
    if routes is None and route_table_path is not None:
        routes = load_config(route_table_path)

    profile = db.get(FlowProfile, flow_code)
    if profile is None:
        profile = FlowProfile(flow_code=flow_code)
        db.add(profile)
    profile.audit_sop = dsl
    profile.mode = mode
    profile.writeback_tier = writeback_tier
    profile.amount_cap = amount_cap
    profile.auto_pass_enabled = auto_pass_enabled
    if routes is not None:
        profile.route_table = routes
    db.flush()
    return profile
