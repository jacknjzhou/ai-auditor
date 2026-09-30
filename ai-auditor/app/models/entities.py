"""SQLAlchemy ORM 实体 —— 与详细设计文档 §2 DDL 对应。

生产环境为 PostgreSQL（JSONB / UUID / TIMESTAMPTZ），与 sql/ddl.sql 保持一致；
为使单测可脱离 PG 运行，此处使用跨库通用类型：
  JSON   —— PG 下自动使用 JSONB 语义（SQLAlchemy 方言映射）
  UUID   —— 跨库 UUID：PG 渲染为原生 UUID 列（与 ddl.sql 一致），SQLite 降级 CHAR(32)；
           读写均归一为 uuid4().hex（32 位无连字符），与 new_id()/frame_id 字符串语义一致
  VECTOR —— PG 渲染为 pgvector 原生 vector(n)（可走 HNSW/SQL 内 RRF），SQLite 降级 JSON；
           Python 侧统一 list[float]，hybrid_search 纯 Python 计算路径不变
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, Uuid, create_engine
# 注意：包级 `sqlalchemy.dialects.postgresql.UUID` 其实是 generic Uuid 的重导出别名；
# PG 方言 colspecs 里真实的 uuid 实现类型是 PGUuid（adapt_type 据此适配），必须从 base 导入
from sqlalchemy.dialects.postgresql.base import PGUuid as _PGUuid
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.types import TypeEngine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from app.config import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# 跨库 UUID / 向量类型（与 sql/ddl.sql 的列类型对齐）
# ---------------------------------------------------------------------------

# 跨库 UUID 读写归一：32 位 hex（无连字符）。
# 标准 Uuid(as_uuid=False) 回读为带连字符的 str(uuid)，而本库代码以 uuid4().hex
# （无连字符）生成/拼接 id（如 frame_id=f"{run_id}:{node_id}"），跨会话回读会格式漂移；
# 此处读写统一归一为 hex，与 new_id() 及既有字符串语义完全一致。
def _uuid_result_process(value):
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value.hex
    if isinstance(value, str):
        try:
            return uuid.UUID(value).hex
        except ValueError:
            # 宽容回退：测试在 SQLite 里用非 uuid 占位串（如 "t-wb"）；
            # PG 原生 UUID 列不可能存入非法值，生产路径不受影响
            return value
    return value


def _uuid_bind_process(value):
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return uuid.UUID(value).hex
        except ValueError:
            return value
    return value.hex if isinstance(value, uuid.UUID) else value


class _UuidHex(Uuid):
    """generic Uuid（as_uuid=False）：SQLite 渲染 CHAR(32)；PG 走下方 with_variant。"""

    def bind_processor(self, dialect):
        return _uuid_bind_process

    def result_processor(self, dialect, coltype):
        return _uuid_result_process


class _PGUuidHex(_PGUuid):
    """PG 原生 UUID（渲染 uuid 列，与 sql/ddl.sql 一致）+ 读写 hex 归一。"""

    def bind_processor(self, dialect):
        return _uuid_bind_process

    def result_processor(self, dialect, coltype):
        return _uuid_result_process


def _uuid() -> TypeEngine:
    """跨库 UUID 列类型。

    必须用 with_variant 注入 _PGUuidHex：psycopg 方言的 colspecs 会把 generic Uuid 子类
    adapt 回 PGUuid（丢失子类覆写），而 adapt_type 对已是 PGUuid 子类的类型直通。
    """
    return _UuidHex(as_uuid=False).with_variant(
        _PGUuidHex(as_uuid=False), "postgresql"
    )


class _PGVector(TypeEngine):
    """pgvector 向量类型（仅 PG 方言使用）：渲染 VECTOR(n)，Python 侧 list[float]。

    psycopg 将 '[0.1,0.2,...]' 文本绑定到 vector 列由服务端解析；
    读回为文本形式，result processor 还原为 list[float]。
    """

    __visit_name__ = "vector"

    def __init__(self, dim: int = 1024):
        super().__init__()
        self.dim = dim

    def bind_processor(self, dialect):
        def process(value):
            if value is None:
                return None
            if isinstance(value, str):
                return value
            return "[" + ",".join(repr(float(x)) for x in value) + "]"
        return process

    def result_processor(self, dialect, coltype):
        def process(value):
            if value is None:
                return None
            if isinstance(value, (list, tuple)):
                return [float(x) for x in value]
            inner = str(value).strip()
            if inner.startswith("["):
                inner = inner[1:]
            if inner.endswith("]"):
                inner = inner[:-1]
            return [float(x) for x in inner.split(",") if x.strip()]
        return process

    def coerce_compared_with(self, other, **kw):  # noqa: ARG002
        return self


@compiles(_PGVector)
def _compile_pgvector(element, compiler, **kw):  # noqa: ARG001
    return f"VECTOR({element.dim})"


def _vector(dim: int = 1024):
    """跨库向量列类型：PG 用 pgvector 原生 vector(n)（可走 HNSW/SQL 内 RRF），
    其他后端（SQLite 测试）降级 JSON。Python 侧统一 list[float] 读写。"""
    return JSON().with_variant(_PGVector(dim), "postgresql")


class ConnectorConfig(Base):
    __tablename__ = "connector_config"
    id: Mapped[str] = mapped_column(_uuid(), primary_key=True, default=new_id)
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
    id: Mapped[str] = mapped_column(_uuid(), primary_key=True, default=new_id)
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
    id: Mapped[str] = mapped_column(_uuid(), primary_key=True, default=new_id)
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
    run_id: Mapped[str] = mapped_column(_uuid(), ForeignKey("audit_task.id"))
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
    run_id: Mapped[str] = mapped_column(_uuid(), ForeignKey("audit_task.id"))
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
    id: Mapped[str] = mapped_column(_uuid(), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(_uuid(), ForeignKey("audit_task.id"))
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
    run_id: Mapped[str] = mapped_column(_uuid(), ForeignKey("audit_task.id"))
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
    id: Mapped[str] = mapped_column(_uuid(), primary_key=True, default=new_id)
    policy_code: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(256))
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | archived
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KnowledgeChunk(Base):
    """制度条款切片。embedding 跨库 Vector 类型：PG 为 pgvector vector(1024)（与
    sql/ddl.sql 一致），SQLite 测试降级 JSON；检索由 HybridRetriever 在 Python 内完成
    （生产可切换 SQL 内 RRF）。"""
    __tablename__ = "knowledge_chunk"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[str] = mapped_column(_uuid(), ForeignKey("knowledge_doc.id"))
    clause_no: Mapped[str] = mapped_column(String(32), default="")
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list] = mapped_column(_vector(1024), default=list)
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
