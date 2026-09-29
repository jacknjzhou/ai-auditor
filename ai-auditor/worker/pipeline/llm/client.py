"""LLM 模型网关客户端 —— OpenAI 兼容协议（NewAPI /v1/chat/completions）。

设计要点（详细设计 §9 / §10）：
- 同步 httpx 调用（Arq worker 进程内执行，无需 asyncio 开销）；
- 重试 llm_retry_times 次，网络/超时/5xx 之间指数退避；
- 强制 JSON 输出：response_format={"type": "json_object"} + 解析失败剥离代码围栏重试；
- 任何失败返回 None，由调用方（审核链）走降级路径，绝不抛异常打断流水线。
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


class LLMClient(Protocol):
    """审核链依赖的最小客户端协议，便于测试注入 stub。"""

    def chat_json(self, system: str, user: str, *, step: str = "") -> dict | None: ...


@dataclass
class CallTrace:
    """单次调用留痕（写入 llm_call 表的内存形态）。"""

    step: str
    model: str
    ok: bool
    latency_ms: int = 0
    degraded: bool = False
    error: str = ""
    response_digest: str = ""
    attempts: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


def _extract_json(text: str) -> dict | None:
    """从模型回复中提取 JSON 对象：先直接解析，失败剥代码围栏，再失败取首个 {...} 块。"""
    if not text:
        return None
    candidates = [text.strip()]
    fenced = _FENCE_RE.sub("", text.strip())
    if fenced != candidates[0]:
        candidates.append(fenced)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


class NewAPIClient:
    """NewAPI / OpenAI 兼容网关客户端（同步）。"""

    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 model: str | None = None, timeout: float | None = None,
                 retry: int | None = None):
        self.base_url = (base_url or settings.llm_base_url).rstrip("/")
        self.api_key = api_key if api_key is not None else settings.llm_api_key
        self.model = model or settings.llm_model
        self.timeout = timeout or settings.llm_timeout_seconds
        self.retry = settings.llm_retry_times if retry is None else retry
        self.last_trace: CallTrace | None = None

    def chat_json(self, system: str, user: str, *, step: str = "") -> dict | None:
        """发送对话并解析 JSON 输出；失败重试，最终失败返回 None。"""
        trace = CallTrace(step=step, model=self.model, ok=False)
        self.last_trace = trace
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        last_err = ""
        for attempt in range(self.retry + 1):
            trace.attempts = attempt + 1
            t0 = time.monotonic()
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.post(f"{self.base_url}/chat/completions",
                                       json=payload, headers=headers)
                trace.latency_ms = int((time.monotonic() - t0) * 1000)
                if resp.status_code >= 500 or resp.status_code == 429:
                    last_err = f"http {resp.status_code}"
                    time.sleep(0.4 * (2 ** attempt))
                    continue
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
                obj = _extract_json(content)
                if obj is None:
                    last_err = "unparseable json"
                    continue
                trace.ok = True
                trace.response_digest = json.dumps(obj, ensure_ascii=False)[:512]
                return obj
            except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
                trace.latency_ms = int((time.monotonic() - t0) * 1000)
                last_err = f"{type(exc).__name__}: {exc}"
                time.sleep(0.4 * (2 ** attempt))
        trace.error = last_err
        trace.degraded = True
        logger.warning("llm call failed (step=%s): %s", step, last_err)
        return None
