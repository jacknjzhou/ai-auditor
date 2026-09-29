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
| LLM 客户端 | `worker/pipeline/llm/client.py` | NewAPI OpenAI 兼容协议；重试+指数退避；JSON 提取（围栏剥离）；CallTrace 留痕 |
| LLM 审核链 | `worker/pipeline/llm/chain.py` | S1 材料→S2 一致性→S3 制度→S4 风险置信；`<materials>` 隔离+注入扫描；问题码白名单归一化；单步降级 |
| 任务派发 | `app/services/dispatcher.py` | queue_mode=inline（默认）|arq（Redis 队列）；arq 失败自动降级 inline |
| Arq Worker | `worker/settings.py` | `arq worker.settings.WorkerSettings`，与 inline 共用同一流水线 |
| RAG 嵌入 | `worker/pipeline/rag/embeddings.py` | NewAPI /v1/embeddings + 离线 HashingEmbedding 兜底；cosine |
| RAG 检索 | `worker/pipeline/rag/retriever.py` | 摄取切片 + 向量×关键词 RRF 融合；**生效期按单据提交日过滤** |
| 路由引擎 | `worker/pipeline/routing/router.py` | route_table 问题码匹配（priority），shadow 恒不动；ADVISORY 仅 COMMENT |
| 回写执行 | `worker/pipeline/routing/writeback.py` | FULL_API→COMMENT_ONLY→IM_ONLY 降级链；EscalationLog 留痕；Mock/Flaky/Http 适配器 |
| 审核流水线 | `app/services/audit_service.py` | 端到端：事件→规则→[RAG+LLM 链]→融合→路由回写落库；先内存聚合后持久化 |
| 数据模型 | `app/models/entities.py` + `sql/ddl.sql` | ORM（测试用 SQLite）+ 生产 PostgreSQL DDL |

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
| `AUDITOR_RAG_ENABLED` | false | 开启 RAG 制度摘录（S3 引用生效期内的条款） |
| `AUDITOR_EMBEDDING_MODEL` | text-embedding-3-small | NewAPI /v1/embeddings 模型；无 Key 时回落哈希嵌入 |

## 目录结构

```
ai-auditor/
├── app/            # FastAPI 应用（API 进程）
│   ├── api/v1/     # webhooks 入站 + 任务查询
│   ├── schemas/    # Canonical Model 标准单据契约
│   ├── models/     # SQLAlchemy 实体（与 sql/ddl.sql 对应）
│   └── services/   # 审核流水线编排
├── worker/         # Worker 进程模块（P1 接 arq 独立进程）
│   └── pipeline/   # rules 规则引擎 / fusion 决策矩阵
├── sql/            # 生产 PostgreSQL DDL
├── deploy/         # systemd unit（详细设计 §13）
└── tests/          # 27 个用例：规则引擎/决策矩阵/webhook
```

## P1 待办（对齐详细设计 §15）

1. ~~Arq Worker 独立进程 + Redis 队列派发~~ ✅ `app/services/dispatcher.py` + `worker/settings.py`（queue_mode=arq，Redis 故障自动降级 inline）
2. ~~LLM 审核链 S1–S4~~ ✅ `worker/pipeline/llm/chain.py` + `client.py`（NewAPI OpenAI 兼容协议；`AUDITOR_LLM_ENABLED=true` 开启；测试注入 stub chain）
3. ~~RAG 知识库摄取与混合检索~~ ✅ `worker/pipeline/rag/`（向量×关键词 RRF；生效期按单据提交日过滤；`AUDITOR_RAG_ENABLED=true` 开启）
4. ~~转人工路由引擎 + 回写执行器~~ ✅ `worker/pipeline/routing/`（route_table 匹配 + 三档回写降级 + escalation_log 留痕；FlowProfile 四档模式含自动通过 gate 与金额上限）
5. 首个真实审批流 Adapter（报销流）对接联调
