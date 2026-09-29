"""回写执行器 —— 对应详细设计 §7.4 回写三档降级。

档位（flow_profile.writeback_tier）：
    FULL_API     直接调用审批系统 API（意见/打回/自动通过）
    COMMENT_ONLY API 能力弱 → 意见附件方式追加
    IM_ONLY      只推 IM 机器人看板，线下兜底

降级链：FULL_API 失败 → COMMENT_ONLY → IM_ONLY，每级失败都记录到
EscalationLog.writeback_response，绝不因回写失败阻塞流水线。

适配器协议 WritebackAdapter 便于测试注入；生产实现：
- HttpWritebackAdapter：POST writeback_config.http_endpoint（Adapter 侧实现协议）；
- ImPushAdapter：推送企业微信/钉钉机器人（P2 接入）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from sqlalchemy.orm import Session

from app.models.entities import EscalationLog, FlowProfile, utcnow
from worker.pipeline.routing.router import RoutePlan

logger = logging.getLogger(__name__)

TIER_ORDER = ["FULL_API", "COMMENT_ONLY", "IM_ONLY"]


@dataclass
class WritebackOutcome:
    status: str  # applied | degraded | failed | skipped_shadow
    tier_used: str = ""
    attempts: list[dict[str, Any]] = field(default_factory=list)


class WritebackAdapter(Protocol):
    def apply(self, tier: str, plan: RoutePlan, task_ctx: dict) -> dict: ...
    """返回 {"ok": bool, "detail": ...}；不抛异常。"""


class MockWritebackAdapter:
    """测试/影子演示用：记录调用，恒成功。"""

    def __init__(self):
        self.calls: list[dict] = []

    def apply(self, tier: str, plan: RoutePlan, task_ctx: dict) -> dict:
        self.calls.append({"tier": tier, "action": plan.action,
                           "target_node": plan.target_node,
                           "task_id": task_ctx.get("task_id")})
        return {"ok": True, "detail": "mock"}


class FlakyWritebackAdapter:
    """指定档位失败，模拟降级链。"""

    def __init__(self, failing_tiers: set[str]):
        self.failing = failing_tiers
        self.calls: list[dict] = []

    def apply(self, tier: str, plan: RoutePlan, task_ctx: dict) -> dict:
        self.calls.append(tier)
        if tier in self.failing:
            return {"ok": False, "detail": f"{tier} endpoint error"}
        return {"ok": True, "detail": "ok"}


class HttpWritebackAdapter:
    """生产 HTTP 适配器：POST 到 connector 侧回写端点。"""

    def __init__(self, endpoint: str, timeout: float = 10.0):
        self.endpoint = endpoint
        self.timeout = timeout

    def apply(self, tier: str, plan: RoutePlan, task_ctx: dict) -> dict:
        try:
            resp = httpx.post(self.endpoint, json={
                "tier": tier, "action": plan.action,
                "target_node": plan.target_node,
                "task_id": task_ctx.get("task_id"),
                "instance_id": task_ctx.get("instance_id"),
                "comment": plan.comment,
                "problem_codes": plan.problem_codes,
            }, timeout=self.timeout)
            ok = resp.status_code < 400
            return {"ok": ok, "detail": f"http {resp.status_code}"}
        except httpx.HTTPError as exc:
            return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}


def _adapter_for(profile: FlowProfile) -> WritebackAdapter:
    endpoint = (profile.writeback_config or {}).get("http_endpoint")
    if endpoint:
        from app.config import settings as _s
        return HttpWritebackAdapter(endpoint, timeout=_s.writeback_http_timeout)
    return MockWritebackAdapter()


def execute_writeback(db: Session, profile: FlowProfile, plan: RoutePlan,
                      task_id: str, instance_id: str,
                      decision_level: str, problem_codes: list[str],
                      *, adapter: WritebackAdapter | None = None) -> WritebackOutcome:
    """按档位降级链执行回写，落 EscalationLog。"""
    log = EscalationLog(task_id=task_id, decision_level=decision_level,
                        problem_codes=problem_codes, target_node=plan.target_node,
                        action=plan.action, writeback_tier=profile.writeback_tier,
                        writeback_status="pending", comment=plan.comment[:2000])
    db.add(log)

    outcome = WritebackOutcome(status="failed")
    impl = adapter or _adapter_for(profile)
    task_ctx = {"task_id": task_id, "instance_id": instance_id}

    start = TIER_ORDER.index(profile.writeback_tier) if profile.writeback_tier in TIER_ORDER else 1
    for tier in TIER_ORDER[start:]:
        res = impl.apply(tier, plan, task_ctx)
        outcome.attempts.append({"tier": tier, **res})
        if res.get("ok"):
            outcome.status = "applied" if tier == TIER_ORDER[start] else "degraded"
            outcome.tier_used = tier
            break
    else:
        outcome.status = "failed"
        outcome.tier_used = "IM_ONLY"

    log.writeback_status = outcome.status
    log.writeback_tier = outcome.tier_used or log.writeback_tier
    log.writeback_response = {"attempts": outcome.attempts,
                              "executed_at": utcnow().isoformat()}
    db.commit()
    logger.info("writeback task=%s status=%s tier=%s attempts=%d",
                task_id, outcome.status, outcome.tier_used, len(outcome.attempts))
    return outcome
