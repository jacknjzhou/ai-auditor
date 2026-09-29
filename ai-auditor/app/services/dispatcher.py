"""任务派发器 —— 对应详细设计 §5.3 进程模型。

queue_mode 配置：
- inline：进程内后台执行（FastAPI BackgroundTasks / 同步调用），开发与测试默认；
- arq：投递到 Redis 队列由 worker 进程消费（生产多实例部署）；
  投递失败（Redis 不可达等）自动降级 inline，保证旁路系统不因队列故障丢任务。
"""
from __future__ import annotations

import logging

from app.config import settings

logger = logging.getLogger(__name__)

_arq_pool = None  # 进程级缓存，避免每次请求建连


async def enqueue_arq(task_id: str) -> bool:
    """尝试投递 Arq 队列；成功 True / 失败 False。"""
    global _arq_pool
    try:
        from arq import create_pool
        from arq.connections import RedisSettings

        if _arq_pool is None:
            _arq_pool = await create_pool(
                RedisSettings.from_dsn(settings.redis_url),
                default_queue_name=settings.arq_queue_name,
            )
        await _arq_pool.enqueue_job("run_audit_task_job", task_id)
        return True
    except Exception as exc:  # noqa: BLE001 —— 队列故障不阻塞入站
        logger.warning("arq enqueue failed, fallback to inline: %s", exc)
        _arq_pool = None
        return False


def run_audit_task_inline(task_id: str) -> None:
    from app.services.audit_service import run_audit_task
    run_audit_task(task_id)


async def dispatch_audit_task(task_id: str, background=None) -> str:
    """按配置派发审核任务，返回实际通道：'arq' | 'inline'。

    - queue_mode=inline：BackgroundTasks 进程内执行（background=None 时同步执行）；
    - queue_mode=arq：先尝试 Redis 队列，失败降级 BackgroundTasks，任务不丢。
    """
    if settings.queue_mode == "arq":
        if await enqueue_arq(task_id):
            return "arq"
        logger.warning("task %s fallback to inline dispatch", task_id)
    if background is not None:
        background.add_task(run_audit_task_inline, task_id)
        return "inline"
    run_audit_task_inline(task_id)
    return "inline"
