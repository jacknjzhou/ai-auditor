"""嵌入提供者 —— NewAPI /v1/embeddings（OpenAI 兼容）+ 离线测试 stub。

生产用 NewAPIEmbedding；测试/无网环境用 HashingEmbedding（字符 n-gram 哈希到
固定维度并归一化，语义上等价于"字面相似度"，足以验证检索链路逻辑）。
"""
from __future__ import annotations

import hashlib
import logging
import math
from typing import Protocol

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class EmbeddingProvider(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class NewAPIEmbedding:
    """经模型网关批量嵌入；失败抛异常由调用方决定降级。"""

    def __init__(self, model: str | None = None, dim: int = 1024):
        self.model = model or settings.embedding_model
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        resp = httpx.post(
            f"{settings.llm_base_url.rstrip('/')}/embeddings",
            headers={"Authorization": f"Bearer {settings.llm_api_key}"} if settings.llm_api_key else {},
            json={"model": self.model, "input": texts},
            timeout=settings.llm_timeout_seconds,
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        return [item["embedding"] for item in data]


class HashingEmbedding:
    """确定性哈希嵌入（离线/测试用）：与 tokenize 同源——CJK 字符 bigram + 英数词。
    dim=1024 与真实嵌入对齐，哈希桶碰撞概率可忽略。"""

    dim = 1024

    def embed(self, texts: list[str]) -> list[list[float]]:
        from worker.pipeline.rag.retriever import tokenize
        out = []
        for text in texts:
            vec = [0.0] * self.dim
            grams = tokenize(text)
            for gram in grams:
                h = int(hashlib.md5(gram.encode()).hexdigest(), 16)
                vec[h % self.dim] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out


def get_embedding_provider() -> EmbeddingProvider:
    """按网关可用性选择提供者：优先 NewAPI，失败回落 Hashing（记录告警）。"""
    if settings.llm_api_key:
        try:
            provider = NewAPIEmbedding()
            provider.embed(["ping"])
            return provider
        except Exception as exc:  # noqa: BLE001
            logger.warning("NewAPI embedding unavailable, fallback to hashing: %s", exc)
    return HashingEmbedding()


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)
