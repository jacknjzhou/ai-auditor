# ai-auditor — 数字人审核系统（P0 骨架）

旁路式审批智能审核中台。设计文档：`../docs/数字人审核系统方案分析.md`（v1.0 方案）与
`../docs/plans/2026-09-28-数字人审核系统-详细设计.md`（v2.0 详细设计）。

## P0 已实现

| 模块 | 位置 | 说明 |
|------|------|------|
| Webhook 入站 | `app/api/v1/webhooks.py` | HMAC-SHA256 + 时间戳防重放 + 幂等键去重，202 异步受理 |
| 规则引擎 | `worker/pipeline/rules/engine.py` | DSL 求值器：and/or/not、比较、in、any_empty、truthy、内置函数；缺失变量 fail-safe；全子句证据轨迹 |
| 内置函数库 | `worker/pipeline/rules/functions.py` | days_between / budget_balance / duplicate_fingerprint / user_in_role / seal_count_today（P0 桩 + std 注入） |
| 决策矩阵 | `worker/pipeline/fusion/matrix.py` | 规则×LLM 置信度融合，硬违规 REJECT、自动通过 gate |
| **内核·帧契约** | `worker/pipeline/kernel/frames.py` | TaskFrame / TaskRequirement（v3.0 设计 §4.1/§4.2）：SOP 静态投影、依赖就绪判定、契约静态校验 |
| **内核·能力注册表** | `worker/pipeline/kernel/capabilities.py` | 六项能力（rule_query / knowledge_search / llm_verify / llm_assess / doc_extract / writeback_probe）包装既有资产；确定性 digest 支撑重试签名 |
| **内核·Harness** | `worker/pipeline/kernel/harness.py` | 串行 tool\|finish 协议 + protocol_repair 修复 + 能力白名单 + 知识预算硬拦截 + retryable=false 同签名禁重 + 强制能力完成门槛 |
| **内核·Planner** | `worker/pipeline/kernel/planner.py` | AuditSOP 校验 + builtin 等价 SOP（零迁移）；确定性展开 SOP→TaskFrame 队列；运行时条件（on_*）二次展开 |
| **内核·执行器** | `app/services/kernel_runner.py` | 内核路径编排：Planner 展开 → Harness 逐帧（确定性 actor）→ 运行时条件解析 → 复用统一决策回写出口；**挂起-恢复 + 增量重审**（human_gate 挂起、resume 复用未变帧） |
| **内核·事件流** | `worker/pipeline/kernel/events.py` | EventRecorder：九类事件白名单（frame_planned/started/capability_called/result/protocol_repair/awaiting_human/frame_finished/composed/writeback_applied/resumed）、seq 单调、flush 幂等、支持 start_seq 续写 |
| **内核·Composer** | `worker/pipeline/kernel/composer.py` | 第三层：帧产出归一（findings/置信度/摘要/风险标记/降级/复用证据），决策矩阵仍是机器结论的最终确定性 gate |
| **内核·SOP 装载** | `worker/pipeline/kernel/sop_registry.py` | **新流零代码接入**：YAML/JSON SOP 装载 + 静态校验 + `flow_profile` upsert（含路由表） |
| **run 观测 API** | `app/api/v1/runs.py` | `GET /runs/{id}/events?after_seq=`（增量游标）、`/capability-calls`（预算/延迟审计）、`POST /audit-tasks/{id}/resume`（HMAC + resume_token 幂等） |
| **SOP 管理 API** | `app/api/v1/sops.py` | `PUT/GET /flows/{code}/sop` + `/sop/validate`（控制台接入面） |
| **示例 SOP** | `config/sops/seal_application.yaml` | 用章申请 SOP（六节点含 human_gate）+ 路由表，演示零代码接入 |
| LLM 客户端 | `worker/pipeline/llm/client.py` | NewAPI OpenAI 兼容协议；重试+指数退避；JSON 提取（围栏剥离）；CallTrace 留痕 |
| LLM 审核链 | `worker/pipeline/llm/chain.py` | S1 材料→S2 一致性→S3 制度→S4 风险置信；`<materials>` 隔离+注入扫描；问题码白名单归一化；单步降级 |
| 任务派发 | `app/services/dispatcher.py` | queue_mode=inline（默认）|arq（Redis 队列）；arq 失败自动降级 inline |
| Arq Worker | `worker/settings.py` | `arq worker.settings.WorkerSettings`，与 inline 共用同一流水线 |
| RAG 嵌入 | `worker/pipeline/rag/embeddings.py` | NewAPI /v1/embeddings + 离线 HashingEmbedding 兜底；cosine |
| RAG 检索 | `worker/pipeline/rag/retriever.py` | 摄取切片 + 向量×关键词 RRF 融合；**生效期按单据提交日过滤** |
| 路由引擎 | `worker/pipeline/routing/router.py` | route_table 问题码匹配（priority），shadow 恒不动；ADVISORY 仅 COMMENT |
| 回写执行 | `worker/pipeline/routing/writeback.py` | FULL_API→COMMENT_ONLY→IM_ONLY 降级链；EscalationLog 留痕；Mock/Flaky/Http 适配器 |
| 审核流水线 | `app/services/audit_service.py` | legacy 路径：事件→规则→[RAG+LLM 链]→融合→路由回写落库；先内存聚合后持久化。决策回写出口 `_decide_and_writeback` 与内核共用 |
| 数据模型 | `app/models/entities.py` + `sql/ddl.sql` | ORM（测试用 SQLite）+ 生产 PostgreSQL DDL |

## 新流零代码接入（v3.0 内核 §5 / §9-T6）

新增一条审批流审核，只需两份配置，**不改动 Runner / Planner / Harness 任何代码**：

```bash
# 1) SOP 状态机（YAML/JSON）：节点 / 转移 / 能力白名单 / 知识预算 / 失败策略
#    示例见 config/sops/seal_application.yaml（用章申请，六节点含 human_gate）
# 2) 路由表：problem_code → 处置动作 / 目标节点
#    示例见 config/sops/seal_application.routes.json
```

```python
from worker.pipeline.kernel.sop_registry import install_flow_sop
install_flow_sop(db, "seal_application",
                 "config/sops/seal_application.yaml",
                 route_table_path="config/sops/seal_application.routes.json",
                 mode="advisory", writeback_tier="COMMENT_ONLY")
db.commit()
```

或走管理 API：`PUT /api/v1/flows/{flow_code}/sop`（内联 DSL，装载期静态校验）。
接入后 `AUDITOR_KERNEL_MODE=true` 即按配置驱动执行；`flow_profile.audit_sop` 为空时
回退内置等价 SOP（零迁移）。端到端验收见 `tests/test_sop_onboarding.py`。

## 挂起-恢复（v2.1 §1 / v3.0 §6）

`human_gate` 帧挂起 → 写 `human_task`（resume_token）+ 任务置 `awaiting_human`（不出决策）；
人工动作经 `POST /api/v1/audit-tasks/{id}/resume` 幂等恢复：

| action | 语义 |
|--------|------|
| `COMMENT` | 已完成帧全复用，从挂起点续跑下游后决策（链结果随 run_cache 还原，零重复 LLM 调用） |
| `SUPPLY_MATERIAL` | `patch_fields` 合并进快照 + 规则全量重算；帧指纹未变者 `reused`（材料类帧重跑、摘录未变帧复用） |
| `OVERRIDE_PASS` / `OVERRIDE_REJECT` | 不经流水线直写终审（`matrix_reason.cell=human_override`） |

一次 run 最多 `AUDITOR_MAX_SUSPEND_ROUNDS`（默认 3）轮挂起，超限强制出决策（防死循环）。
验收见 `tests/test_resume.py`。

## 快速开始

```bash
# 测试（无需 PG/Redis，内存 SQLite）
python -m pytest tests/ -v

# 本地启动
uvicorn app.main:app --port 8300
# 健康检查: http://localhost:8300/healthz
```

发送一条事件（签名算法见 `tests/conftest.py::sign`）：

```python
import hashlib, hmac, json, time, httpx
payload = {"source_system": "oa-a8", "event_type": "NODE_ARRIVED", "version": 0,
           "instance": {"instance_id": "AP1", "flow_code": "expense_reimburse",
                        "current_node": "fin_review",
                        "form": {"amount": 500000, "trip_reason": "客户支持"},
                        "applicant": {"user_id": "u1", "name": "张三"}}}
raw = json.dumps(payload).encode()
ts = str(int(time.time()))
sig = hmac.new(b"dev-secret", ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
r = httpx.post("http://localhost:8300/api/v1/webhooks/oa-a8", content=raw,
               headers={"X-Auditor-Signature": "sha256=" + sig,
                        "X-Auditor-Timestamp": ts})
print(r.json())   # {"task_id": "...", "deduplicated": false, "status": "accepted"}
```

查询任务：`GET /api/v1/audit-tasks/{task_id}`（status/findings/decision）。

## 容器化部署（Docker Compose 全套）

一键起全栈：PostgreSQL(pgvector) + Redis + 自动建库 + 自动装载 SOP + API×2 + Worker×2 + Nginx。

```bash
cd deploy
cp .env.example .env      # 修改全部 change-me 密钥
make up                   # 或 docker compose -f docker-compose.yml up -d --build
make ps && curl http://localhost:8300/healthz
```

- **自动建库**：postgres 数据卷为空时自动执行 `sql/ddl.sql`（pgcrypto + pgvector 扩展、HNSW 索引）；
- **一次性 `migrate` 服务**：`python -m app.cli init --seed` —— 建表 + 装载 `config/sops/*.yaml`
  到 `flow_profile` + 演示种子（幂等），成功退出后 api/worker 才启动；
- **控制台原型**：Nginx 托管于 `http://localhost:8300/console/`；
- **运维命令**：`make load-sops`（改 SOP 免重建镜像）、`make check`（数据层自检）、
  `make backup/restore/psql/scale-worker`；详见 `deploy/README-container.md`。

裸机（systemd）方案见 `deploy/auditor-api.service`、`deploy/auditor-worker@.service`，
与容器化二选一，共用同一套环境变量与 CLI（`python -m app.cli init|load-sops|check|seed`）。

## 配置（环境变量前缀 AUDITOR_）

| 变量 | 默认 | 说明 |
|------|------|------|
| `AUDITOR_DATABASE_URL` | PostgreSQL 本库 | 测试用 `sqlite://` |
| `AUDITOR_WEBHOOK_SECRET_DEFAULT` | dev-secret | HMAC 密钥（每 connector 可单独配置） |
| `AUDITOR_LLM_BASE_URL` | NewAPI 网关 | P1 审核链 S1–S4 调用入口 |
| `AUDITOR_LLM_ENABLED` | false | 开启 LLM 审核链（开启前先配置 API Key 并跑通影子评测） |
| `AUDITOR_LLM_MODEL` | gpt-4o-mini | NewAPI 网关侧的模型名 |
| `AUDITOR_QUEUE_MODE` | inline | inline（进程内后台）\| arq（Redis 队列，生产用） |
| `AUDITOR_REDIS_URL` | redis://localhost:6379/0 | arq 模式的 Redis 地址 |
| `AUDITOR_KERNEL_MODE` | false | v3.0 内核执行（Planner SOP 展开 → Harness 逐帧 → Composer 归一）；false 走 legacy 硬编码流水线。等价迁移验收见 `tests/test_kernel_pipeline.py`；SOP 挂 `flow_profile.audit_sop`（NULL=内置等价 SOP） |
| `AUDITOR_MAX_SUSPEND_ROUNDS` | 3 | 一次 run 最多挂起-恢复轮次（防 human_gate 死循环，超限强制出决策） |
| `AUDITOR_SOP_MODE` / `AUDITOR_SOP_WRITEBACK_TIER` | advisory / COMMENT_ONLY | `config/sops/*.yaml` 装载时**新建** flow 的默认放权档位（已存在 flow 保留运营侧调整，`--force` 可覆盖） |
| `AUDITOR_RAG_ENABLED` | false | 开启 RAG 制度摘录（S3 引用生效期内的条款） |
| `AUDITOR_EMBEDDING_MODEL` | text-embedding-3-small | NewAPI /v1/embeddings 模型；无 Key 时回落哈希嵌入 |

## 目录结构

```
ai-auditor/
├── app/            # FastAPI 应用（API 进程）
│   ├── api/v1/     # webhooks 入站 + 任务查询 + run 事件/挂起恢复 + SOP 管理
│   ├── cli.py      # 运维 CLI：init / load-sops / seed / check（容器 init 服务同源）
│   ├── schemas/    # Canonical Model 标准单据契约
│   ├── models/     # SQLAlchemy 实体（与 sql/ddl.sql 对应）
│   └── services/   # 审核流水线编排（legacy + kernel_runner 内核）
├── worker/         # Worker 进程模块（P1 接 arq 独立进程）
│   └── pipeline/   # rules 规则引擎 / fusion 决策矩阵 / kernel 三层内核 / llm / rag / routing
├── config/sops/    # 示例 SOP（YAML）+ 路由表（JSON）—— 新流零代码接入
├── sql/            # 生产 PostgreSQL DDL（pgcrypto + pgvector）
├── deploy/         # Dockerfile / docker-compose（一键全栈）/ nginx / Makefile / systemd
└── tests/          # 129 个用例：规则/矩阵/webhook/内核等价迁移/事件流/挂起恢复/零代码接入/CLI
```

## P1 待办（对齐详细设计 §15）

1. ~~Arq Worker 独立进程 + Redis 队列派发~~ ✅ `app/services/dispatcher.py` + `worker/settings.py`（queue_mode=arq，Redis 故障自动降级 inline）
2. ~~LLM 审核链 S1–S4~~ ✅ `worker/pipeline/llm/chain.py` + `client.py`（NewAPI OpenAI 兼容协议；`AUDITOR_LLM_ENABLED=true` 开启；测试注入 stub chain）
3. ~~RAG 知识库摄取与混合检索~~ ✅ `worker/pipeline/rag/`（向量×关键词 RRF；生效期按单据提交日过滤；`AUDITOR_RAG_ENABLED=true` 开启）
4. ~~转人工路由引擎 + 回写执行器~~ ✅ `worker/pipeline/routing/`（route_table 匹配 + 三档回写降级 + escalation_log 留痕；FlowProfile 四档模式含自动通过 gate 与金额上限）
5. 首个真实审批流 Adapter（报销流）对接联调
