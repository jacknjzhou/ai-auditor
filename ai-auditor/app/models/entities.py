"""SQLAlchemy ORM 实体 —— 与详细设计文档 §2 DDL 对应。

生产环境为 PostgreSQL（JSONB / UUID / TIMESTAMPTZ）；
为使单测可脱离 PG 运行，此处使用跨库通用类型：
  JSON  —— PG 下自动使用 JSONB 语义（SQLAlchemy 方言映射）
  UUID  —— 以 String(36) 存储，生成用 uuid4 hex
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from app.config import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class ConnectorConfig(Base):
    __tablename__ = "connector_config"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_code: Mapped[str] = mapped_column(String(32), unique=True)
    protocol: Mapped[str] = mapped_column(String(16))  # webhook | polling | mq
    adapter_class: Mapped[str] = mapped_column(String(64), default="mock")
    webhook_secret: Mapped[str] = mapped_column(String(128), default="")
    field_mapping: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class ApprovalEvent(Base):
    __tablename__ = "approval_event"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_code: Mapped[str] = mapped_column(String(32))
    instance_id: Mapped[str] = mapped_column(String(64))
    node_code: Mapped[str] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    version_no: Mapped[int] = mapped_column(Integer, default=0)
    raw_payload: Mapped[dict] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditTask(Base):
    __tablename__ = "audit_task"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    event_id: Mapped[int] = mapped_column(ForeignKey("approval_event.id"))
    source_code: Mapped[str] = mapped_column(String(32))
    instance_id: Mapped[str] = mapped_column(String(64))
    node_code: Mapped[str] = mapped_column(String(64))
    flow_code: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), default="received")
    mode: Mapped[str] = mapped_column(String(16), default="shadow")
    error_info: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditFinding(Base):
    __tablename__ = "audit_finding"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("audit_task.id"))
    problem_code: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(12))
    engine: Mapped[str] = mapped_column(String(16), default="rule")
    title: Mapped[str] = mapped_column(String(256))
    detail: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    rule_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditDecision(Base):
    __tablename__ = "audit_decision"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("audit_task.id"))
    level: Mapped[str] = mapped_column(String(16))  # PASS | ADVISORY | ESCALATE | REJECT
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    matrix_reason: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PolicyRule(Base):
    __tablename__ = "policy_rule"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    flow_code: Mapped[str] = mapped_column(String(64))  # '*' 全局
    rule_code: Mapped[str] = mapped_column(String(64))
    rule_name: Mapped[str] = mapped_column(String(128), default="")
    problem_code: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(12), default="minor")
    dsl: Mapped[dict] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (Index("ix_rule_flow", "flow_code", "rule_code", "version_no"),)


class LlmCall(Base):
    """LLM 调用留痕 —— 审计与降级分析用（详细设计 §10.4）。"""
    __tablename__ = "llm_call"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("audit_task.id"))
    step: Mapped[str] = mapped_column(String(8))  # S1 | S2 | S3 | S4
    model: Mapped[str] = mapped_column(String(64), default="")
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    degraded: Mapped[bool] = mapped_column(Boolean, default=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    response_digest: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FlowProfile(Base):
    """审批流运行档案：模式/阈值/抽检/路由表/回写档位（详细设计 §6 / §12）。"""
    __tablename__ = "flow_profile"
    flow_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    mode: Mapped[str] = mapped_column(String(16), default="shadow")  # shadow|advisory|semi_auto|full_auto
    auto_pass_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    amount_cap: Mapped[float | None] = mapped_column(Float, nullable=True)  # 自动通过金额上限
    sample_rate: Mapped[float] = mapped_column(Float, default=0.0)  # 自动通过随机抽检比例
    route_table: Mapped[list] = mapped_column(JSON, default=list)
    # route_table item: {"problem_codes": ["MISSING_RECEIPT"], "target_node": "applicant",
    #                    "action": "RETURN|ESCALATE|COMMENT", "priority": 1}
    writeback_tier: Mapped[str] = mapped_column(String(16), default="COMMENT_ONLY")
    # FULL_API | COMMENT_ONLY | IM_ONLY
    writeback_config: Mapped[dict] = mapped_column(JSON, default=dict)
    # 审核 SOP 状态机（v3.0 内核，设计 §5）：None = 内置等价 SOP（零迁移）
    audit_sop: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)


class EscalationLog(Base):
    """转人工/回写执行记录（详细设计 §12 escalation_log）。"""
    __tablename__ = "escalation_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("audit_task.id"))
    decision_level: Mapped[str] = mapped_column(String(16))
    problem_codes: Mapped[list] = mapped_column(JSON, default=list)
    target_node: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(16), default="")  # RETURN|ESCALATE|COMMENT|SAMPLE_CHECK
    writeback_tier: Mapped[str] = mapped_column(String(16), default="")
    writeback_status: Mapped[str] = mapped_column(String(16), default="pending")
    # pending | applied | degraded | failed | skipped_shadow
    writeback_response: Mapped[dict] = mapped_column(JSON, default=dict)
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RunEvent(Base):
    """run_event 全链路埋点（v3.0 内核设计 §6/§7，T4）。

    Planner/Harness/Composer 三层统一事件流：(run_id, seq) 唯一，seq 单调递增；
    事件类型：frame_planned / frame_started / capability_called / capability_result
    / protocol_repair / awaiting_human / frame_finished / composed / writeback_applied。
    增量查询 API 先行（GET /api/v1/runs/{run_id}/events?after_seq=），SSE 留 P4。
    """
    __tablename__ = "run_event"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("audit_task.id"))
    seq: Mapped[int] = mapped_column(Integer)  # (run_id, seq) 唯一
    event_type: Mapped[str] = mapped_column(String(32))
    frame_id: Mapped[str] = mapped_column(String(80), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (Index("uq_run_event_run_seq", "run_id", "seq", unique=True),)


class CapabilityCallRecord(Base):
    """capability_call 能力调用留痕（v3.0 设计 §7）：预算审计与延迟画像。

    与 run_event.capability_result 同源，独立成表便于预算/延迟聚合审计；
    budget_left = 该次调用完成后帧内知识预算余量（仅 budgeted 能力有意义）。
    """
    __tablename__ = "capability_call"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("audit_task.id"))
    frame_id: Mapped[str] = mapped_column(String(80))
    capability: Mapped[str] = mapped_column(String(64))
    arguments_digest: Mapped[str] = mapped_column(Text, default="")
    result_digest: Mapped[str] = mapped_column(Text, default="")
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    retryable: Mapped[bool] = mapped_column(Boolean, default=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    budget_left: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (Index("ix_capcall_run", "run_id", "frame_id"),)


class HumanTask(Base):
    """human_task 待办（v2.1 增补设计 §1.2 / v3.0 §6，T5）——从 escalation_log 分离的挂起语义。

    挂起：human_gate 帧 finish awaiting_human → 写本表 + task.status=awaiting_human；
    恢复：人工动作（SUPPLY_MATERIAL/OVERRIDE_PASS/OVERRIDE_REJECT/COMMENT）经
    resume_token 幂等恢复（重复回调不产生二次恢复）；result 记录人工决议全量留痕。
    run_cache 为 run 级瞬态（增量重审的链结果缓存等），闭单后可清理。
    """
    __tablename__ = "human_task"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("audit_task.id"))
    seq: Mapped[int] = mapped_column(Integer, default=0)  # 挂起时的 run_event seq
    frame_id: Mapped[str] = mapped_column(String(80), default="")
    node_id: Mapped[str] = mapped_column(String(64), default="")
    problem_codes: Mapped[list] = mapped_column(JSON, default=list)
    assignee_role: Mapped[str] = mapped_column(String(64), default="")
    assignee_user: Mapped[str | None] = mapped_column(String(64), nullable=True)
    options: Mapped[list] = mapped_column(JSON, default=list)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|done|expired|cancelled
    resume_token: Mapped[str] = mapped_column(String(64), unique=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    run_cache: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TaskFrameRecord(Base):
    """task_frame 帧档案（v3.0 设计 §7，T5）——帧级执行留痕 + 增量重审复用基础。

    outcome JSON = FrameOutcome.to_dict() + {"fingerprint": ...}：指纹按帧 kind
    覆盖其真实输入（规则帧=scope、llm_verify=snapshot、llm_assess=规则结论+摘录、
    knowledge_check=rag query、writeback=回写档位），恢复时指纹未变的帧直接复用
    上轮结果（reused=true），不重复执行能力、不重复调用 LLM、不重复落 findings。
    """
    __tablename__ = "task_frame"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("audit_task.id"))
    seq: Mapped[int] = mapped_column(Integer)  # (run_id, seq) 唯一
    frame_id: Mapped[str] = mapped_column(String(80))
    node_id: Mapped[str] = mapped_column(String(64), default="")
    kind: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(24), default="queued")
    requirement: Mapped[dict] = mapped_column(JSON, default=dict)
    depends_on: Mapped[list] = mapped_column(JSON, default=list)
    outcome: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (Index("uq_task_frame_run_seq", "run_id", "seq", unique=True),)


class KnowledgeDoc(Base):
    """制度文档（版本化，生效期按单据提交日过滤）。"""
    __tablename__ = "knowledge_doc"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    policy_code: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(256))
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | archived
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KnowledgeChunk(Base):
    """制度条款切片。生产为 pgvector vector(1024)+tsvector；ORM 侧 embedding 用 JSON
    存储向量，检索由 HybridRetriever 在 Python 内完成（生产可切换 SQL 内 RRF）。"""
    __tablename__ = "knowledge_chunk"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[str] = mapped_column(ForeignKey("knowledge_doc.id"))
    clause_no: Mapped[str] = mapped_column(String(32), default="")
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list] = mapped_column(JSON, default=list)
    tokens: Mapped[list] = mapped_column(JSON, default=list)  # 关键词索引（简化 tsvector）


def _make_engine():
    """生产：PostgreSQL 直连；测试：内存 SQLite（跨线程共享连接）。"""
    url = settings.database_url
    if url.startswith("sqlite"):
        from sqlalchemy.pool import StaticPool
        kwargs: dict = {"connect_args": {"check_same_thread": False}}
        if ":memory:" in url or url == "sqlite://":
            kwargs["poolclass"] = StaticPool
        return create_engine(url, future=True, **kwargs)
    return create_engine(url, future=True)


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    Base.metadata.create_all(engine)
