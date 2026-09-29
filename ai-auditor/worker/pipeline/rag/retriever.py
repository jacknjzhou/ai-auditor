"""制度知识库：摄取切片 + 混合检索（向量×关键词 RRF 融合）。

关键合规细节（详细设计 §8.4）：制度生效期按【单据提交日】过滤，
而不是当前日期——保证历史单据按当时有效的制度审核。

检索实现说明：
- ORM/测试路径：Python 内计算余弦 + 关键词命中，RRF 融合排序（本文件）；
- 生产优化路径：pgvector cosine_ops + tsvector，SQL 内 RRF（见 sql/ddl.sql 注释），
  接口一致可替换。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from sqlalchemy.orm import Session

from app.models.entities import KnowledgeChunk, KnowledgeDoc
from worker.pipeline.rag.embeddings import EmbeddingProvider, cosine

logger = logging.getLogger(__name__)


@dataclass
class PolicyExcerpt:
    doc_title: str
    policy_code: str
    clause_no: str
    text: str
    score: float = 0.0
    vec_rank: int = 0
    kw_rank: int = 0

    def as_dict(self) -> dict:
        return {"policy": self.doc_title, "policy_code": self.policy_code,
                "clause": self.clause_no, "text": self.text, "score": round(self.score, 4)}


@dataclass
class IngestResult:
    doc_id: str
    n_chunks: int


def tokenize(text: str) -> list[str]:
    """极简中文分词：字符 bigram + 英文/数字词。生产可换 jieba。"""
    tokens = set(re.findall(r"[a-zA-Z0-9]+", text))
    zh = re.sub(r"[^\u4e00-\u9fff]", "", text)
    tokens.update(re.findall(r"..", zh))
    return sorted(tokens)


def ingest_document(
    db: Session,
    provider: EmbeddingProvider,
    *,
    policy_code: str,
    title: str,
    version_no: int = 1,
    effective_from: datetime | None = None,
    effective_to: datetime | None = None,
    clauses: list[dict],
) -> IngestResult:
    """摄取一份制度文档：clauses=[{"clause_no": "第8条", "content": "..."}]。

    覆盖式摄取：同 policy_code+version 的旧切片先删除（版本演进）。
    """
    from app.models.entities import new_id

    doc = (db.query(KnowledgeDoc)
           .filter_by(policy_code=policy_code, version_no=version_no).first())
    if doc is None:
        doc = KnowledgeDoc(id=new_id(), policy_code=policy_code)
        db.add(doc)
    doc.title = title
    doc.version_no = version_no
    doc.effective_from = effective_from or datetime(2020, 1, 1, tzinfo=timezone.utc)
    doc.effective_to = effective_to
    doc.status = "active"
    db.flush()

    (db.query(KnowledgeChunk).filter_by(doc_id=doc.id).delete())
    db.flush()

    contents = [c["content"] for c in clauses]
    vectors = provider.embed(contents)
    for clause, vec in zip(clauses, vectors):
        db.add(KnowledgeChunk(
            doc_id=doc.id,
            clause_no=str(clause.get("clause_no", "")),
            content=clause["content"],
            embedding=list(vec),
            tokens=tokenize(clause["content"]),
        ))
    db.commit()
    return IngestResult(doc_id=doc.id, n_chunks=len(clauses))


@dataclass
class RetrievalResult:
    excerpts: list[PolicyExcerpt] = field(default_factory=list)
    degraded: bool = False
    note: str = ""


def _naive(dt: datetime) -> datetime:
    """统一为 naive UTC 比较（SQLite 回读 naive，PG 回读 aware）。"""
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def hybrid_search(
    db: Session,
    provider: EmbeddingProvider,
    *,
    query: str,
    submitted_on: date | None,
    top_k: int | None = None,
    rrf_k: int | None = None,
) -> RetrievalResult:
    """混合检索：向量相似 + 关键词命中 → RRF 融合；按单据提交日过滤生效期。"""
    from app.config import settings as _s

    top_k = top_k or _s.rag_top_k
    rrf_k = rrf_k if rrf_k is not None else _s.rag_rrf_k
    if submitted_on is None:
        submitted_on = date.today()

    day_from = datetime(submitted_on.year, submitted_on.month, submitted_on.day)
    docs = (db.query(KnowledgeDoc)
            .filter(KnowledgeDoc.status == "active")
            .all())
    # 生效期过滤在 Python 侧做，规避 SQLite naive/PG aware 混比问题
    docs = [d for d in docs
            if _naive(d.effective_from) <= day_from
            and (d.effective_to is None or _naive(d.effective_to) >= day_from)]
    if not docs:
        return RetrievalResult(degraded=True, note="no active policy docs for date")

    doc_ids = [d.id for d in docs]
    chunks = db.query(KnowledgeChunk).filter(KnowledgeChunk.doc_id.in_(doc_ids)).all()
    if not chunks:
        return RetrievalResult(degraded=True, note="no chunks")

    doc_map = {d.id: d for d in docs}
    try:
        qvec = provider.embed([query])[0]
    except Exception as exc:  # noqa: BLE001 —— 嵌入失败降级为纯关键词
        qvec = None
        logger.warning("query embedding failed, keyword-only: %s", exc)

    qtokens = set(tokenize(query))

    vec_scored: list[tuple[float, KnowledgeChunk]] = []
    kw_scored: list[tuple[float, KnowledgeChunk]] = []
    for ch in chunks:
        if qvec is not None and ch.embedding:
            vec_scored.append((cosine(qvec, ch.embedding), ch))
        overlap = len(qtokens & set(ch.tokens or []))
        kw_scored.append((overlap, ch))

    def rrf(ranking: list[tuple[float, KnowledgeChunk]]) -> dict[int, float]:
        # 按分数降序取排名，RRF = Σ 1/(k + rank)
        ranking = sorted(ranking, key=lambda x: x[0], reverse=True)
        out: dict[int, float] = {}
        for rank, (_, ch) in enumerate(ranking, start=1):
            if ch.id not in out:
                out[ch.id] = 0.0
            out[ch.id] += 1.0 / (rrf_k + rank)
        return out

    vec_rrf = rrf(vec_scored) if vec_scored else {}
    kw_rrf = rrf(kw_scored)
    fused: dict[int, float] = {}
    for cid in {**vec_rrf, **kw_rrf}:
        fused[cid] = vec_rrf.get(cid, 0.0) + kw_rrf.get(cid, 0.0)

    chunk_map = {c.id: c for c in chunks}
    order = sorted(fused.items(), key=lambda x: x[1], reverse=True)[:top_k]
    excerpts = []
    max_score = fused[order[0][0]] if order else 1.0
    for cid, score in order:
        ch = chunk_map[cid]
        doc = doc_map[ch.doc_id]
        excerpts.append(PolicyExcerpt(
            doc_title=doc.title, policy_code=doc.policy_code,
            clause_no=ch.clause_no,
            text=ch.content[:_s.rag_max_excerpt_chars],
            score=score / max_score if max_score else 0.0,
        ))
    return RetrievalResult(excerpts=excerpts)
