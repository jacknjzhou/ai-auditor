"""ai-auditor FastAPI 应用入口。

启动：uvicorn app.main:app --host 0.0.0.0 --port 8300
健康检查：GET /healthz
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1.webhooks import router
from app.config import settings
from app.models.entities import init_db


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title=settings.app_name, version="0.4.2-p0", lifespan=lifespan)
app.include_router(router, prefix="/api/v1")


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "app": settings.app_name}
