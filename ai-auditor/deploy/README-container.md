# ai-auditor 容器化部署方案（Docker Compose 全套服务）

> 版本 2026-09-30 · 对应代码 P3.0（三层内核 / 挂起恢复 / 声明式 SOP 接入）
> 一键起全栈：PostgreSQL(pgvector) + Redis + 自动建库 + 自动装载 SOP + API×2 + Worker×2 + Nginx(+控制台)

## 1. 拓扑

```
                        ┌──────────────── 宿主机 :8300 ────────────────┐
  审批系统 webhook ──►  │                   Nginx                      │
                        │         /            → least_conn 反代        │
                        │         /console/    → 控制台原型（静态）      │
                        │         /healthz     → 探活                    │
                        └───────────────┬──────────────────────────────┘
                                ┌───────┴────────┐
                          ┌─────▼────┐      ┌────▼─────┐
                          │  api-1   │      │  api-2    │  验签 / 幂等 / 建任务 → 202
                          └─────┬────┘      └────┬─────┘
                                └── enqueue ─────┘
                          ┌──────────────────────────┐
                          │      Redis 7 (AOF)       │  arq 队列 auditor
                          └──────┬────────────┬──────┘
                          ┌──────▼────┐  ┌────▼──────┐
                          │ worker-1  │  │ worker-2  │  规则 → LLM 链 → 决策矩阵 → 回写
                          └──────┬────┘  └────┬──────┘        （可 --scale 扩容）
                          ┌──────▼────────────▼──────┐
                          │  PostgreSQL 16 + pgvector │  任务/决策/留痕/知识库/事件流
                          └───────────▲──────────────┘
                                      │ 建库后执行一次（幂等）
                          ┌───────────┴──────────────┐
                          │  migrate（一次性 init）    │  python -m app.cli init --seed
                          └──────────────────────────┘
```

- **API 只做受理**（HMAC 验签 + 时间戳防重放 + 幂等去重 + 建任务 + 入队），毫秒级返回 202；
- **审核执行全部在 Worker**（`AUDITOR_QUEUE_MODE=arq`）；Worker 故障只延迟不丢任务
  （Redis AOF 持久化 + 投递失败自动降级 inline 兜底）；
- **`migrate` 一次性服务**：PG 就绪后执行「建表 + 装载 `config/sops/*.yaml` + 演示种子」，
  成功退出后 api/worker 才启动——保证服务起来时 SOP 已就位；
- 双 API + 双 Worker 多活，任一实例宕机 Nginx / arq 自动摘除。

## 2. 快速开始

```bash
cd ai-auditor/deploy
cp .env.example .env          # ① 修改全部 change-me 密钥（必做）
make up                       # ② 构建 + 启动全套（等价 docker compose -f docker-compose.yml up -d --build）
make ps                       # ③ 查看健康
curl http://localhost:8300/healthz
```

不使用 `make` 时，等价的 compose 原生命令：

```bash
cd ai-auditor/deploy
cp .env.example .env
docker compose -f docker-compose.yml up -d --build

# 首次建库无需手动执行 —— postgres 数据卷为空时自动跑 sql/ddl.sql（含 pgvector）；
# 装载 SOP / 种子由 migrate 服务完成（幂等，可重跑）：
docker compose -f docker-compose.yml run --rm --no-deps migrate

docker compose -f docker-compose.yml ps
curl http://localhost:8300/healthz
```

启动后：

| 地址 | 说明 |
|------|------|
| `http://localhost:8300/healthz` | 健康检查 |
| `http://localhost:8300/console/` | 数字人审核控制台原型 |
| `http://localhost:8300/api/v1/...` | 业务 API（webhook 入站 / 任务查询 / 事件流 / SOP 管理 / resume） |
| `http://localhost:8301` | Adminer（需 `--profile tools`） |

> **首次建库的两种情况**
> - 全新环境（无数据卷）：postgres 容器首次启动自动执行 `sql/ddl.sql`，开箱即用；
> - 沿用旧数据卷（此前手工建过库）：initdb 不会重跑，需手动补 DDL：
>   `docker compose -f docker-compose.yml exec -T postgres psql -U auditor -d auditor < ../sql/ddl.sql`
>   （DDL 全部 `CREATE TABLE` 不带 IF NOT EXISTS，重复执行会报错，属预期；`CREATE EXTENSION IF NOT EXISTS` 幂等）

## 3. 服务清单

| 服务 | 镜像 | 作用 | 副本 |
|------|------|------|------|
| `postgres` | `pgvector/pgvector:pg16` | 主库 + 向量检索；自动执行 `sql/ddl.sql` | 1 |
| `redis` | `redis:7-alpine` | arq 队列（AOF + requirepass） | 1 |
| `migrate` | 本项目镜像 | 一次性：建表 + 装载 SOP + 种子（幂等） | 跑完即退 |
| `api-1` / `api-2` | 本项目镜像 | FastAPI：验签/幂等/建任务/入队/查询 | 2 |
| `worker-1` / `worker-2` | 本项目镜像 | arq worker：执行审核流水线 | 2（可 --scale） |
| `nginx` | `nginx:1.27-alpine` | least_conn 反代 + 控制台静态托管 | 1 |
| `adminer` | `adminer:4` | 数据库调试（`--profile tools`） | 0/1 |

## 4. 环境变量（deploy/.env）

| 变量 | 必改 | 默认 | 说明 |
|------|:---:|------|------|
| `POSTGRES_PASSWORD` | ✅ | — | PG 密码（`${VAR:?}` 缺失即启动失败） |
| `REDIS_PASSWORD` | ✅ | — | Redis requirepass；连接串 `redis://:<pwd>@redis:6379/0` |
| `AUDITOR_WEBHOOK_SECRET_DEFAULT` | ✅ | — | HMAC 兜底密钥；正式接入每 connector 单独配 |
| `AUDITOR_EXPOSE_PORT` | | 8300 | 宿主机暴露端口 |
| `AUDITOR_ADMINER_PORT` | | 8301 | Adminer 端口（`--profile tools`） |
| `AUDITOR_KERNEL_MODE` | | false | `true` 启用 Planner→Harness→Composer 三层内核 |
| `AUDITOR_MAX_SUSPEND_ROUNDS` | | 3 | human_gate 挂起-恢复轮次上限 |
| `AUDITOR_SOP_MODE` / `_WRITEBACK_TIER` | | advisory / COMMENT_ONLY | **新建** flow 的默认放权档位 |
| `AUDITOR_LLM_ENABLED` | | false | 开链开关（先影子评测再放量） |
| `AUDITOR_LLM_BASE_URL` | | `http://10.26.8.43:3002/v1` | NewAPI 网关（OpenAI 兼容） |
| `AUDITOR_LLM_API_KEY` | 开链前必填 | — | 网关密钥 |
| `AUDITOR_RAG_ENABLED` | | false | 制度摘录检索开关 |
| `AUDITOR_CONF_HIGH` / `_LOW` | | 0.90 / 0.60 | 决策矩阵置信度阈值 |

## 5. 一键运维（deploy/Makefile）

```bash
make help            # 列出全部命令
make up              # 构建 + 启动全套
make ps / make logs  # 状态 / 跟踪日志
make init            # 重跑初始化（建表 + 装载 SOP + 种子，幂等）
make load-sops       # 改完 config/sops/*.yaml 后重新装载（免重建镜像）
make check           # 数据层连通自检（PG + Redis）
make shell-api       # 进入 api-1 shell
make psql            # 打开生产库 psql
make redis-cli       # 打开 redis-cli（带鉴权）
make backup          # 备份到 deploy/backups/
make restore F=backups/auditor-xxx.sql.gz
make scale-worker    # Worker 扩容到 4
make reset           # ⚠️ 删除数据卷并重建（清库）
```

**滚动升级**：`make build && docker compose -f docker-compose.yml up -d`（仅重建有变化的容器；
Worker 属长任务进程，升级会等当前 job 完成或超时）。

## 6. 健康检查与自愈

| 组件 | 检查方式 | 故障行为 |
|------|---------|---------|
| api ×2 | `GET /healthz`（容器内 urllib） | unhealthy → depends_on 阻塞 + Nginx `max_fails` 摘除 |
| worker ×2 | `arq --check`（连 Redis 心跳） | unhealthy 人工排查；任务仍在队列不丢 |
| postgres | `pg_isready` | api/worker/migrate 启动阻塞至就绪 |
| redis | `redis-cli -a … ping` | 同上；enqueue 失败自动降级 inline |
| nginx | `wget /healthz` | — |

启动顺序由 `depends_on` 条件保证：
`postgres/redis(healthy)` → `migrate(completed_successfully)` → `api(healthy)` → `nginx`。

## 7. 新审批流接入（零代码）

```bash
# ① 放入配置（SOP 状态机 YAML + 可选同名路由表 JSON）
vim config/sops/reimbursement.yaml
vim config/sops/reimbursement.routes.json

# ② 装载（幂等；已存在 flow 保留其运营侧 mode/tier）
make load-sops
#   或指定单流：docker compose -f docker-compose.yml run --rm --no-deps migrate \
#                 python -m app.cli load-sops --force

# ③ 验证
curl http://localhost:8300/api/v1/flows/reimbursement/sop
```

`config/sops/` 以只读卷挂载进容器，改配置**无需重建镜像**；装载期做静态校验
（节点唯一 / 转移目标存在 / 能力声明非空），坏配置不进运行库。

## 8. 安全要点

- 数据库 / Redis 端口**不对宿主机暴露**（调试时用 `127.0.0.1:` 前缀临时放开）；
- 全部密钥走 `deploy/.env`（已在 `.dockerignore` 排除，不进镜像层），生产配合 secret 管理；
- 镜像以非 root 用户 `auditor` 运行，`PYTHONUNBUFFERED=1` 日志实时输出；
- 必填密钥用 `${VAR:?...}` 快速失败，避免「空密码启动」；
- Nginx 仅作内网入口，TLS 建议由上层网关终止，或在本文件再加 `443` server 块；
- 开 LLM 链前：确认网关连通 → 影子模式收集一致率 ≥85% → 再逐步放权。

## 9. 与 systemd 方案的关系

`deploy/auditor-api.service`、`deploy/auditor-worker@.service`（非容器化多活）与容器化方案
**二选一**，两者共用同一套环境变量语义与同一套 CLI（`python -m app.cli init|load-sops|check`），
可随时迁移。

## 10. 故障排查

| 现象 | 原因 / 处置 |
|------|------------|
| `postgres` 反复重启，日志含 `extension "vector" is not available` | 误用了官方 postgres 镜像；须用 `pgvector/pgvector:pg16` |
| api/worker 报 `NOAUTH Authentication required` | Redis 开了 requirepass 但连接串缺密码；确认 `AUDITOR_REDIS_URL=redis://:<pwd>@redis:6379/0` |
| `migrate` 退出码非 0 | 看日志：多为 SOP 静态校验失败（坏 YAML / 转移指向不存在的节点），修 `config/sops/` 后 `make init` |
| api 一直 `unhealthy`，`up` 卡住 | `migrate` 未成功完成；先 `docker compose run --rm migrate` 看输出 |
| 改 SOP 后不生效 | SOP 从库读取，需 `make load-sops` 重新装载（改的是文件不是库） |
| 旧数据卷缺新表（如 run_event） | initdb 不重跑；手动执行 `sql/ddl.sql` 中缺失的 `CREATE TABLE`，或 `make reset`（清库重建） |
