-- ============================================================
-- ai-auditor P0 PostgreSQL DDL（生产库）
-- 与详细设计文档 §2 对应；ORM 实体见 app/models/entities.py
-- 测试环境使用内存 SQLite 由 ORM 自动建表，此文件仅用于生产 PG。
-- ============================================================
CREATE EXTENSION IF NOT EXISTS pgcrypto;
-- P1 追加: CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE connector_config (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_code     VARCHAR(32) UNIQUE NOT NULL,
    protocol        VARCHAR(16) NOT NULL,
    adapter_class   VARCHAR(64) NOT NULL DEFAULT 'mock',
    webhook_secret  VARCHAR(128) NOT NULL DEFAULT '',
    field_mapping   JSONB NOT NULL DEFAULT '{}',
    enabled         BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE approval_event (
    id              BIGSERIAL PRIMARY KEY,
    source_code     VARCHAR(32) NOT NULL,
    instance_id     VARCHAR(64) NOT NULL,
    node_code       VARCHAR(64) NOT NULL,
    event_type      VARCHAR(32) NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL,
    version_no      INT NOT NULL DEFAULT 0,
    raw_payload     JSONB NOT NULL,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (idempotency_key)
);
CREATE INDEX idx_event_instance ON approval_event(source_code, instance_id);

CREATE TABLE audit_task (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id        BIGINT NOT NULL REFERENCES approval_event(id),
    source_code     VARCHAR(32) NOT NULL,
    instance_id     VARCHAR(64) NOT NULL,
    node_code       VARCHAR(64) NOT NULL,
    flow_code       VARCHAR(64) NOT NULL,
    snapshot        JSONB NOT NULL,
    status          VARCHAR(24) NOT NULL DEFAULT 'received',
    mode            VARCHAR(16) NOT NULL DEFAULT 'shadow',
    error_info      JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ
);
CREATE INDEX idx_task_status ON audit_task(status)
    WHERE status IN ('received','rule_eval','fusion');

CREATE TABLE audit_finding (
    id            BIGSERIAL PRIMARY KEY,
    task_id       UUID NOT NULL REFERENCES audit_task(id),
    problem_code  VARCHAR(64) NOT NULL,
    severity      VARCHAR(12) NOT NULL,
    engine        VARCHAR(16) NOT NULL DEFAULT 'rule',
    title         VARCHAR(256) NOT NULL,
    detail        TEXT NOT NULL DEFAULT '',
    evidence      JSONB NOT NULL DEFAULT '[]',
    rule_code     VARCHAR(64),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_finding_task ON audit_finding(task_id);

CREATE TABLE audit_decision (
    id             BIGSERIAL PRIMARY KEY,
    task_id        UUID NOT NULL REFERENCES audit_task(id),
    level          VARCHAR(16) NOT NULL,
    confidence     NUMERIC(4,3),
    matrix_reason  JSONB NOT NULL DEFAULT '{}',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE policy_rule (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    flow_code    VARCHAR(64) NOT NULL,
    rule_code    VARCHAR(64) NOT NULL,
    rule_name    VARCHAR(128) NOT NULL DEFAULT '',
    problem_code VARCHAR(64) NOT NULL,
    severity     VARCHAR(12) NOT NULL DEFAULT 'minor',
    dsl          JSONB NOT NULL,
    enabled      BOOLEAN NOT NULL DEFAULT true,
    version_no   INT NOT NULL DEFAULT 1,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (flow_code, rule_code, version_no)
);

CREATE TABLE human_feedback (
    id             BIGSERIAL PRIMARY KEY,
    task_id        UUID NOT NULL REFERENCES audit_task(id),
    actor_user     VARCHAR(64) NOT NULL,
    action         VARCHAR(24) NOT NULL,
    original_level VARCHAR(16) NOT NULL,
    final_level    VARCHAR(16) NOT NULL,
    comment        TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- P1.1: LLM 调用留痕（与 ORM 实体 LlmCall 对应；审计与降级分析用）
CREATE TABLE llm_call (
    id              SERIAL PRIMARY KEY,
    task_id         UUID NOT NULL REFERENCES audit_task(id),
    step            VARCHAR(8) NOT NULL,           -- S1 | S2 | S3 | S4
    model           VARCHAR(64) NOT NULL DEFAULT '',
    ok              BOOLEAN NOT NULL DEFAULT FALSE,
    degraded        BOOLEAN NOT NULL DEFAULT FALSE,
    latency_ms      INTEGER NOT NULL DEFAULT 0,
    attempts        INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    response_digest TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_llm_call_task ON llm_call(task_id, step);

-- P1.3: 制度知识库（生产用 pgvector + tsvector；ORM 路径 embedding/tokens 存 JSON）
CREATE TABLE knowledge_doc (
    id              UUID PRIMARY KEY,
    policy_code     VARCHAR(64) NOT NULL,
    title           VARCHAR(256) NOT NULL,
    version_no      INTEGER NOT NULL DEFAULT 1,
    effective_from  TIMESTAMPTZ NOT NULL,
    effective_to    TIMESTAMPTZ,
    status          VARCHAR(16) NOT NULL DEFAULT 'active',  -- active | archived
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (policy_code, version_no)
);

CREATE TABLE knowledge_chunk (
    id              SERIAL PRIMARY KEY,
    doc_id          UUID NOT NULL REFERENCES knowledge_doc(id) ON DELETE CASCADE,
    clause_no       VARCHAR(32) NOT NULL DEFAULT '',
    content         TEXT NOT NULL,
    embedding       vector(1024),          -- 需 CREATE EXTENSION vector
    tokens          JSONB NOT NULL DEFAULT '[]',
    tsv             tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED
);
CREATE INDEX ix_chunk_vec ON knowledge_chunk USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ix_chunk_tsv ON knowledge_chunk USING gin (tsv);
CREATE INDEX ix_chunk_doc ON knowledge_chunk(doc_id);

-- 生产混合检索（RRF 融合，按单据提交日过滤生效期）：
--   WITH vec AS (SELECT id, rank() OVER (ORDER BY embedding <=> $qvec) r
--                FROM knowledge_chunk WHERE doc_id IN (生效中的 doc)),
--        kw  AS (SELECT id, rank() OVER (ORDER BY tsv.rank DESC) r
--                FROM knowledge_chunk WHERE tsv @@ $qtsv AND doc_id IN (...))
--   SELECT id, COALESCE(1.0/(60+vec.r),0)+COALESCE(1.0/(60+kw.r),0) score ... ORDER BY score DESC;

-- P1.4: 审批流运行档案与转人工/回写记录
CREATE TABLE flow_profile (
    flow_code        VARCHAR(64) PRIMARY KEY,
    mode             VARCHAR(16) NOT NULL DEFAULT 'shadow',  -- shadow|advisory|semi_auto|full_auto
    auto_pass_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    amount_cap       NUMERIC(14,2),
    sample_rate      NUMERIC(5,4) NOT NULL DEFAULT 0,
    route_table      JSONB NOT NULL DEFAULT '[]',
    writeback_tier   VARCHAR(16) NOT NULL DEFAULT 'COMMENT_ONLY',
    writeback_config JSONB NOT NULL DEFAULT '{}',
    audit_sop        JSONB,                                   -- 审核 SOP 状态机（v3.0 内核）；NULL = 内置等价 SOP
    version_no       INTEGER NOT NULL DEFAULT 1,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE escalation_log (
    id                SERIAL PRIMARY KEY,
    task_id           UUID NOT NULL REFERENCES audit_task(id),
    decision_level    VARCHAR(16) NOT NULL,
    problem_codes     JSONB NOT NULL DEFAULT '[]',
    target_node       VARCHAR(64),
    action            VARCHAR(16),          -- RETURN|ESCALATE|COMMENT|SAMPLE_CHECK
    writeback_tier    VARCHAR(16),
    writeback_status  VARCHAR(16) NOT NULL DEFAULT 'pending',
    writeback_response JSONB NOT NULL DEFAULT '{}',
    comment           TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_esc_task ON escalation_log(task_id);

-- P3.0-T4: run_event 全链路埋点（Planner/Harness/Composer 三层统一事件流）
CREATE TABLE run_event (
    id          SERIAL PRIMARY KEY,
    run_id      UUID NOT NULL REFERENCES audit_task(id),
    seq         INTEGER NOT NULL,
    event_type  VARCHAR(32) NOT NULL,
    frame_id    VARCHAR(80) NOT NULL DEFAULT '',
    payload     JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_run_event_run_seq UNIQUE (run_id, seq)
);

-- P3.0-T4: capability_call 能力调用留痕（预算/延迟审计）
CREATE TABLE capability_call (
    id               SERIAL PRIMARY KEY,
    run_id           UUID NOT NULL REFERENCES audit_task(id),
    frame_id         VARCHAR(80) NOT NULL,
    capability       VARCHAR(64) NOT NULL,
    arguments_digest TEXT NOT NULL DEFAULT '',
    result_digest    TEXT NOT NULL DEFAULT '',
    ok               BOOLEAN NOT NULL DEFAULT FALSE,
    retryable        BOOLEAN NOT NULL DEFAULT TRUE,
    latency_ms       INTEGER NOT NULL DEFAULT 0,
    budget_left      INTEGER,
    error            TEXT NOT NULL DEFAULT '',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_capcall_run ON capability_call(run_id, frame_id);

-- P3.0-T5: human_task 待办（挂起-恢复；从 escalation_log 分离"待办"语义）
CREATE TABLE human_task (
    id            UUID PRIMARY KEY,
    run_id        UUID NOT NULL REFERENCES audit_task(id),
    seq           INTEGER NOT NULL DEFAULT 0,       -- 挂起时的 run_event seq
    frame_id      VARCHAR(80) NOT NULL DEFAULT '',
    node_id       VARCHAR(64) NOT NULL DEFAULT '',
    problem_codes JSONB NOT NULL DEFAULT '[]',
    assignee_role VARCHAR(64) NOT NULL DEFAULT '',
    assignee_user VARCHAR(64),
    options       JSONB NOT NULL DEFAULT '[]',
    due_at        TIMESTAMPTZ,
    status        VARCHAR(16) NOT NULL DEFAULT 'pending',  -- pending|done|expired|cancelled
    resume_token  VARCHAR(64) NOT NULL UNIQUE,
    result        JSONB,
    run_cache     JSONB,                            -- run 级瞬态（增量重审链结果缓存）
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ
);
CREATE INDEX ix_human_task_run ON human_task(run_id);

-- P3.0-T5: task_frame 帧档案（帧级执行留痕 + 增量重审复用基础）
CREATE TABLE task_frame (
    id          BIGSERIAL PRIMARY KEY,
    run_id      UUID NOT NULL REFERENCES audit_task(id),
    seq         INTEGER NOT NULL,
    frame_id    VARCHAR(80) NOT NULL,
    node_id     VARCHAR(64) NOT NULL DEFAULT '',
    kind        VARCHAR(24) NOT NULL,
    status      VARCHAR(24) NOT NULL DEFAULT 'queued',
    requirement JSONB NOT NULL DEFAULT '{}',
    depends_on  JSONB NOT NULL DEFAULT '[]',
    outcome     JSONB,                              -- FrameOutcome + fingerprint/reused
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    CONSTRAINT uq_task_frame_run_seq UNIQUE (run_id, seq)
);
