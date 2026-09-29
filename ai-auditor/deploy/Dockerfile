# ai-auditor 容器镜像 —— 多阶段构建（builder 装依赖 → runtime 精简运行）
FROM python:3.13-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt \
    && pip install --no-cache-dir --prefix=/install "psycopg[binary]"

FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/install/bin:$PATH \
    PYTHONPATH=/install/lib/python3.13/site-packages:/app

COPY --from=builder /install /install

# 非 root 运行
RUN useradd --system --create-home --shell /usr/sbin/nologin auditor
WORKDIR /app
COPY app/ ./app/
COPY worker/ ./worker/
COPY sql/ ./sql/
RUN chown -R auditor:auditor /app

USER auditor
EXPOSE 8300

# 默认启动 API；worker 服务在 compose 中覆盖 command
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8300"]
