"""Arq Worker 进程设置 —— systemd unit auditor-worker@.service 的入口。

启动：arq worker.settings.WorkerSettings
任务：run_audit_task_job(task_id) → 与 inline 通道共用同一流水线实现。
"""
from __future__ import annotations

from app.config import settings


async def run_audit_task_job(ctx, task_id: str) -> None:
    from app.services.audit_service import run_audit_task
    run_audit_task(task_id)


class WorkerSettings:
    functions = [run_audit_task_job]
    redis_settings = None  # 延迟从 settings 解析，避免 import 期连接
    # 消费队列名必须与派发端（app/services/dispatcher.py enqueue_arq 的
    # default_queue_name）一致，否则任务投进队列后 worker 永远消费不到
    queue_name = settings.arq_queue_name
    max_jobs: int = 8
    job_timeout: int = 120
    health_check_interval: int = 10

    def __init__(self):
        pass

    @classmethod
    def get_redis_settings(cls):
        from arq.connections import RedisSettings
        return RedisSettings.from_dsn(settings.redis_url)


# arq CLI 通过模块级变量读取 settings，需在 import 时可解析
def _resolve():
    from arq.connections import RedisSettings
    WorkerSettings.redis_settings = RedisSettings.from_dsn(settings.redis_url)


_resolve()
