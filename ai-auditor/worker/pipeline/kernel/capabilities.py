"""能力注册表（Capability Registry）—— v3.0 内核设计 §4.4。

设计要点：
- 能力是**确定性 Python 函数**（非 prompt）：即使 llm_verify/llm_assess 内部调 LLM，
  能力调用协议本身零幻觉（结构化入参/出参 + 白名单校验 + 调用留痕）。
- 与 StaffDeck 的差异（设计 §3 差异②）：**白名单制**——Harness 只能调用当前帧
  required/optional_capabilities 声明的能力，不提供全目录自主发现（catalog 仅用于
  给模型看"本帧可选什么"，不做 search）。
- 预算：knowledge_search 声明 budgeted=True，由 Harness 记数并硬性拦截。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class CapabilityCall:
    """单次能力调用留痕（写入 capability_call 表 / run_event）。"""

    name: str
    arguments_digest: str
    ok: bool = False
    result_digest: str = ""
    error: str = ""
    retryable: bool = True
    latency_ms: int = 0
    budget_consumed: bool = False

    def signature(self) -> str:
        """重试判定签名：相同能力 + 相同入参。"""
        return f"{self.name}|{self.arguments_digest}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "ok": self.ok, "error": self.error,
            "retryable": self.retryable, "latency_ms": self.latency_ms,
            "budget_consumed": self.budget_consumed,
            "arguments_digest": self.arguments_digest,
            "result_digest": self.result_digest,
        }


@dataclass
class CapabilitySpec:
    """能力契约：名称 + 描述 + 入参 schema（对齐 StaffDeck capability_describe 语义）。"""

    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    budgeted: bool = False
    kind: str = "builtin"  # builtin | llm | knowledge | tool

    def catalog_entry(self) -> dict[str, Any]:
        """紧凑目录条目：只含名称/类型/描述（不含 schema，省 token）。"""
        return {"name": self.name, "kind": self.kind, "description": self.description}

    def describe_entry(self) -> dict[str, Any]:
        """完整描述：含 input_schema（激活后可见）。"""
        return {**self.catalog_entry(), "input_schema": self.input_schema,
                "budgeted": self.budgeted}


@dataclass
class CapabilityContext:
    """能力执行上下文：由流水线注入的运行时依赖（能力实现不自行取全局状态）。"""

    db: Any = None
    snapshot: dict[str, Any] = field(default_factory=dict)
    scope: dict[str, Any] = field(default_factory=dict)
    flow_code: str = ""
    submitted_on: Any = None
    rule_findings: list[Any] = field(default_factory=list)
    llm_chain: Any = None
    embedding_provider: Any = None
    writeback_tier: str = "FULL_API"
    cache: dict[str, Any] = field(default_factory=dict)
    scratch: dict[str, Any] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)

    def cache_get(self, key: str) -> Any:
        return self.cache.get(key)

    def cache_set(self, key: str, value: Any) -> None:
        self.cache[key] = value


CapabilityHandler = Callable[[CapabilityContext, dict[str, Any]], dict[str, Any]]


def digest(value: Any, limit: int = 200) -> str:
    """入参/出参摘要：稳定短哈希 + 长度，避免把大对象写进事件流。

    必须保持确定性（同输入同输出）——Harness 依赖它构造「同签名重试」判定键。
    """
    import hashlib

    try:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        payload = str(value)
    h = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"sha256:{h[:16]}/len={len(payload)}"[:limit]


# 视为可重试的异常（网络/超时类）；其余按不可重试处理
_RETRYABLE_EXC_HINTS = ("timeout", "timed out", "connection", "temporarily", "503", "429")


def _is_retryable(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(hint in text for hint in _RETRYABLE_EXC_HINTS)


class UnknownCapabilityError(Exception):
    pass


class CapabilityRegistry:
    """能力注册表：注册 / 查询 / 目录 / 调用（含留痕与异常归一化）。"""

    def __init__(self) -> None:
        self._specs: dict[str, CapabilitySpec] = {}
        self._handlers: dict[str, CapabilityHandler] = {}

    # ---------- 注册 ----------
    def register(self, spec: CapabilitySpec, handler: CapabilityHandler) -> None:
        if spec.name in self._specs:
            raise ValueError(f"能力重复注册: {spec.name}")
        self._specs[spec.name] = spec
        self._handlers[spec.name] = handler

    # ---------- 查询 ----------
    def names(self) -> list[str]:
        return sorted(self._specs)

    def has(self, name: str) -> bool:
        return name in self._specs

    def spec(self, name: str) -> CapabilitySpec:
        if name not in self._specs:
            raise UnknownCapabilityError(name)
        return self._specs[name]

    def catalog(self, names: list[str] | None = None) -> list[dict[str, Any]]:
        """紧凑目录（只含允许清单，不泄露其他能力；保持调用方声明的顺序）。"""
        target = list(names) if names is not None else self.names()
        out: list[dict[str, Any]] = []
        for name in target:
            spec = self._specs.get(name)
            if spec is None:
                out.append({"name": name, "kind": "unavailable",
                            "description": "该能力未注册"})
            else:
                out.append(spec.catalog_entry())
        return out

    def describe(self, names: list[str]) -> dict[str, Any]:
        """批量激活：返回完整 schema；未注册的能力进入 unavailable_references。"""
        available, unavailable = [], []
        for name in names:
            spec = self._specs.get(name)
            (available if spec else unavailable).append(
                spec.describe_entry() if spec else name)
        return {"available": available, "unavailable_references": unavailable}

    # ---------- 调用 ----------
    def call(self, name: str, ctx: CapabilityContext,
             arguments: dict[str, Any]) -> tuple[CapabilityCall, dict[str, Any] | None]:
        """执行能力。返回 (留痕记录, 结果)；异常归一化为 ok=False 的记录。"""
        started = time.perf_counter()
        args = arguments if isinstance(arguments, dict) else {}
        record = CapabilityCall(name=name, arguments_digest=digest(args), ok=False)

        spec = self._specs.get(name)
        if spec is None:
            record.error = f"未知能力: {name}"
            record.retryable = False
            return record, None

        try:
            result = self._handlers[name](ctx, args)
            if not isinstance(result, dict):
                result = {"value": result}
            record.ok = True
            record.result_digest = digest(result)
            return record, result
        except Exception as exc:  # noqa: BLE001 —— 统一归一化为能力调用失败
            record.error = f"{type(exc).__name__}: {exc}"[:500]
            record.retryable = _is_retryable(exc)
            logger.warning("capability %s failed: %s", name, record.error)
            return record, None
        finally:
            record.latency_ms = int((time.perf_counter() - started) * 1000)


# ---------------------------------------------------------------------------
# 内置能力实现（包装既有资产，见设计 §8 保留资产映射表）
# ---------------------------------------------------------------------------

def _cap_rule_query(ctx: CapabilityContext, args: dict[str, Any]) -> dict[str, Any]:
    """规则查询：返回已求值的规则 findings；支持 ad-hoc DSL 求值（复用同一 engine）。"""
    from worker.pipeline.rules.engine import RuleEngine
    from worker.pipeline.rules.functions import make_default_functions

    payload: dict[str, Any] = {
        "findings": [f.model_dump() if hasattr(f, "model_dump") else f
                     for f in ctx.rule_findings],
        "n_findings": len(ctx.rule_findings),
    }
    dsl = args.get("dsl")
    if isinstance(dsl, dict):
        engine = RuleEngine(make_default_functions())
        res = engine.evaluate_rule(dsl, ctx.scope or {})
        payload["dsl_result"] = {"hit": res.hit, "evidence": list(res.trail)}
    else:
        payload["scope_keys"] = {
            root: sorted(v)[:20] if isinstance(v, dict) else type(v).__name__
            for root, v in (ctx.scope or {}).items()
        }
    return payload


def _cap_knowledge_search(ctx: CapabilityContext, args: dict[str, Any]) -> dict[str, Any]:
    """制度知识检索：RAG 混合检索（生效期按单据提交日过滤，预算由 Harness 记账）。"""
    from worker.pipeline.rag.retriever import hybrid_search

    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("knowledge_search 需要 query")
    if ctx.db is None or ctx.embedding_provider is None:
        return {"excerpts": [], "degraded": True,
                "note": "知识库未接入（db/embedding provider 缺失）"}

    result = hybrid_search(ctx.db, ctx.embedding_provider,
                           query=query, submitted_on=ctx.submitted_on)
    excerpts = [e.as_dict() for e in result.excerpts]
    ctx.scratch.setdefault("policy_excerpts", []).extend(excerpts)
    return {"excerpts": excerpts, "degraded": result.degraded, "note": result.note}


def _run_chain(ctx: CapabilityContext, args: dict[str, Any]):
    """LLM 链结果缓存：llm_verify 与 llm_assess 在同一次 run 内共享一次调用。"""
    cached = ctx.cache_get("llm_chain_result")
    if cached is not None:
        return cached
    chain = ctx.llm_chain
    if chain is None:
        return None
    excerpts = ctx.scratch.get("policy_excerpts") or args.get("policy_excerpts")
    result = chain.run(ctx.snapshot, ctx.rule_findings, excerpts)
    ctx.cache_set("llm_chain_result", result)
    return result


def _cap_llm_verify(ctx: CapabilityContext, args: dict[str, Any]) -> dict[str, Any]:
    """材料核验 + 一致性核对（原 S1/S2 拆解为能力）。"""
    result = _run_chain(ctx, args)
    if result is None:
        return {"findings": [], "degraded": True, "note": "LLM 未启用或链路不可用"}
    steps = [s for s in result.steps if s.step in ("S1", "S2")]
    return {
        "findings": [f.model_dump() for s in steps for f in s.findings],
        "degraded": any(s.degraded for s in steps),
        "notes": [s.notes for s in steps if s.notes],
    }


def _cap_llm_assess(ctx: CapabilityContext, args: dict[str, Any]) -> dict[str, Any]:
    """制度符合性 + 风险评估（原 S3/S4 拆解为能力），产出置信度信号。"""
    result = _run_chain(ctx, args)
    if result is None:
        return {"findings": [], "confidence": None, "degraded": True,
                "note": "LLM 未启用或链路不可用"}
    steps = [s for s in result.steps if s.step in ("S3", "S4")]
    return {
        "findings": [f.model_dump() for s in steps for f in s.findings],
        "confidence": result.confidence,
        "risk_flags": list(result.risk_flags),
        "summary": result.summary,
        "cited_clauses": list(result.cited_clauses),
        "degraded": any(s.degraded for s in steps),
    }


def _cap_doc_extract(ctx: CapabilityContext, args: dict[str, Any]) -> dict[str, Any]:
    """附件要素抽取（P1.5 挂载点）：当前只报告预抽取结果可用性。"""
    ocr = (ctx.snapshot or {}).get("ocr_struct") or {}
    return {"extracted": bool(ocr), "fields": sorted(ocr)[:30],
            "note": "" if ocr else "尚无预抽取要素（OCR 管道 P1.5 接入）"}


def _cap_writeback_probe(ctx: CapabilityContext, args: dict[str, Any]) -> dict[str, Any]:
    """回写通道能力探测：返回当前 flow 配置的回写档位（不产生副作用）。"""
    tier = args.get("tier") or ctx.writeback_tier
    tiers = ["FULL_API", "COMMENT_ONLY", "IM_ONLY"]
    return {"tier": tier, "available_tiers": tiers,
            "degradable": tier != "IM_ONLY"}


def default_registry() -> CapabilityRegistry:
    """构建默认能力注册表（六项，见设计 §4.4 能力注册表）。"""
    reg = CapabilityRegistry()
    reg.register(
        CapabilitySpec(
            name="rule_query",
            description="查询已求值的规则结论/证据轨迹，或对单据执行一次 DSL 条件求值",
            input_schema={"type": "object", "properties": {
                "dsl": {"type": "object", "description": "可选：ad-hoc 规则 DSL 条件"}}},
            kind="builtin",
        ), _cap_rule_query)
    reg.register(
        CapabilitySpec(
            name="knowledge_search",
            description="检索现行制度条款（生效期按单据提交日过滤，受知识预算约束）",
            input_schema={"type": "object", "required": ["query"], "properties": {
                "query": {"type": "string", "description": "检索问题，如 住宿费标准"}}},
            budgeted=True, kind="knowledge",
        ), _cap_knowledge_search)
    reg.register(
        CapabilitySpec(
            name="llm_verify",
            description="材料核验与一致性核对（发票/行程单/表单字段交叉验证）",
            input_schema={"type": "object", "properties": {}},
            kind="llm",
        ), _cap_llm_verify)
    reg.register(
        CapabilitySpec(
            name="llm_assess",
            description="制度符合性判断与风险评估，输出总体置信度与风险标记",
            input_schema={"type": "object", "properties": {}},
            kind="llm",
        ), _cap_llm_assess)
    reg.register(
        CapabilitySpec(
            name="doc_extract",
            description="读取附件预抽取要素（OCR 结构化结果）",
            input_schema={"type": "object", "properties": {}},
            kind="tool",
        ), _cap_doc_extract)
    reg.register(
        CapabilitySpec(
            name="writeback_probe",
            description="探测审批系统回写通道能力档位（FULL_API/COMMENT_ONLY/IM_ONLY）",
            input_schema={"type": "object", "properties": {
                "tier": {"type": "string"}}},
            kind="tool",
        ), _cap_writeback_probe)
    return reg
