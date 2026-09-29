# ai-auditor 容器化部署方案

> 版本 2026-09-29 · 对应代码 P1（队列派发 + LLM 审核链 + RAG + 路由回写）

## 1. 拓扑

```
                    ┌────────────┐
  审批系统 webhook ──►   Nginx    │ :8300 (least_conn)
                    └─────┬──────┘
                    ┌─────┴──────┐
              ┌─────▼───┐  ┌────▼────┐
              │ api-1   │  │ api-2   │   FastAPI：验签/幂等/建任务 → 202
              └─────┬───┘  └────┬────┘
                    │  enqueue  │
              ┌─────▼───────────▼────┐
              │        Redis (AOF)   │   arq 队列 auditor
              └─────┬───────────┬────┘
              ┌─────▼───┐  ┌────▼────┐
              │ worker-1│  │ worker-2│   规则→LLM链→融合→路由回写（可 scale）
              └─────┬───┘  └────┬────┘
              ┌─────▼───────────▼────┐
              │  PostgreSQL + pgvector│   任务/决策/留痕/知识库
              └──────────────────────┘
```

- **API 只做受理**（HMAC 验签 + 幂等去重 + 建任务 + 入队，毫秒级返回 202）；
- **审核执行全部在 Worker**（`AUDITOR_QUEUE_MODE=arq`），Worker 故障只延迟不丢任务（Redis AOF 持久化 + 投递失败自动降级 inline 兜底）；
- 双 API + 双 Worker 多活，任一实例宕机 Nginx/arq 自动摘除。

## 2. 快速开始

```bash
cd ai-auditor
cp deploy/.env.example deploy/.env       # 修改全部 change-me 密钥
docker compose -f deploy/docker-compose.yml up -d --build

# 首次建库（容器内执行生产 DDL，含 pgvector 扩展与索引）
docker compose -f deploy/docker-compose.yml exec -T postgres \
  psql -U auditor -d auditor < sql/ddl.sql

# 验证
curl http://localhost:8300/healthz
docker compose -f deploy/docker-compose.yml ps
```

> 注意：`init_db()` 在 API 启动时会自动建 ORM 表（SQLite/PG 通用结构）；生产建议以
> `sql/ddl.sql` 为准（含 pgvector / 生成列 / HNSW 索引），二者幂等可共存。

## 3. 环境变量（deploy/.env）

| 变量 | 必改 | 说明 |
|------|------|------|
| `POSTGRES_PASSWORD` | ✅ | PG 密码 |
| `REDIS_PASSWORD` | ✅ | Redis 密码（requirepass 开启） |
| `AUDITOR_WEBHOOK_SECRET_DEFAULT` | ✅ | HMAC 兜底密钥；正式接入每个 connector 单独配 |
| `AUDITOR_EXPOSE_PORT` | 可选 | 宿主机暴露端口，默认 8300 |
| `AUDITOR_LLM_BASE_URL` | 可选 | NewAPI 网关，默认 `http://10.26.8.43:3002/v1` |
| `AUDITOR_LLM_API_KEY` | 开链前必填 | 模型网关密钥 |
| `AUDITOR_LLM_ENABLED` | 可选 | `true` 开启 LLM 审核链（先影子评测再开） |
| `AUDITOR_RAG_ENABLED` | 可选 | `true` 开启制度摘录检索 |

## 4. 日常运维

```bash
# —— 扩容 Worker（LLM 链耗时任务堆积时）——
docker compose -f deploy/docker-compose.yml up -d --scale worker=4 \
  --no-recreate worker-1   # 注意：显式定义的服务扩容用 profile 方式更佳，见下方说明

# —— 滚动升级 ——
docker compose -f deploy/docker-compose.yml build
docker compose -f deploy/docker-compose.yml up -d     # 只重建有变化的容器

# —— 查看审核日志 ——
docker compose -f deploy/docker-compose.yml logs -f worker-1
docker compose -f deploy/docker-compose.yml logs -f api-1

# —— 数据库备份 / 恢复 ——
docker compose -f deploy/docker-compose.yml exec postgres \
  pg_dump -U auditor auditor | gzip > backup-$(date +%F).sql.gz
gunzip -c backup-2026-09-29.sql.gz | \
  docker compose -f deploy/docker-compose.yml exec -T postgres psql -U auditor -d auditor

# —— 队列积压观测 ——
docker compose -f deploy/docker-compose.yml exec redis \
  redis-cli -a "$REDIS_PASSWORD" llen auditor
```

**扩容说明**：compose 内显式定义了 `worker-1/worker-2` 保证基线双副本；如需更多
Worker，推荐 `docker compose --profile scale up -d`（在 compose 中给 worker-2 加
`profiles: [scale]` 后按需拉起），或直接改用 `docker compose up -d --scale` 配合将
worker 定义合并为单个服务名。当前以显式双副本为基线，扩容方式二选一。

## 5. 健康检查与自愈

| 组件 | 检查方式 | 故障行为 |
|------|---------|---------|
| api ×2 | `GET /healthz`（容器内 python urllib） | unhealthy → depends_on 阻断 + `max_fails` 摘除 |
| worker ×2 | `arq --check`（连 Redis 心跳） | unhealthy 人工排查；任务仍在队列不丢 |
| postgres | `pg_isready` | API/Worker 启动阻塞至就绪 |
| redis | `redis-cli ping` | 同上；enqueue 失败自动降级 inline |
| nginx | wget /healthz | — |

## 6. 安全要点

- 数据库/Redis 端口**不对宿主机暴露**（调试时用 `127.0.0.1` 前缀临时放开）；
- 全部密钥走 `.env`（已在 `.dockerignore`/镜像层排除），生产配合 secret 管理；
- 镜像以非 root 用户 `auditor` 运行，`PYTHONUNBUFFERED` 保证日志实时输出；
- Nginx 仅作内网入口，TLS 建议由上层网关（或再加一层 `443` server 块）终止；
- `AUDITOR_LLM_ENABLED` 开启前：先确认网关连通，跑影子模式收集一致率 ≥85% 再放量。

## 7. 与 systemd 方案的关系

`deploy/auditor-api.service`、`deploy/auditor-worker@.service`（非容器化多活方案）
与本次容器化方案**二选一**即可，两者共用同一套环境变量语义，可随时迁移。
