"""规则引擎 DSL 求值器 —— 对应详细设计 §6。

DSL 结构：
    {
      "rule_code": "travel_hotel_cap",
      "when": {"op": "and", "clauses": [ <clause>, ... ]},
      "then": {"problem_code": "...", "severity": "major",
               "message": "住宿单价 {0} 超过标准 {1}",
               "message_args": ["$attachment.ocr.avg_hotel_price", "$std.hotel_cap"]}
    }

clause 形态：
    {"op": "and"|"or", "clauses": [...]}
    {"op": "not", "clause": <clause>}
    {"op": "eq|neq|gt|gte|lt|lte|in|not_in", "left": <operand>, "right": <operand>}
    {"op": "any_empty", "left": ["$form.a", ...]}
    {"op": "truthy", "left": <operand>}

operand 形态：
    字面量 | "$a.b.c" 变量路径 | {"fn": "name", "args": [...]} 内置函数

变量前缀约定（scope 根键）：$form.* / $attachment.* / $ctx.* / $std.*
缺失变量在比较子句中按「未命中」处理（fail-safe），并在轨迹中标注。
"""
from __future__ import annotations

import operator
from dataclasses import dataclass, field
from typing import Any, Callable

from app.schemas.canonical import FindingOut


class UnknownFunctionError(Exception):
    pass


@dataclass
class TrailEntry:
    """证据轨迹：每条子句的求值记录，写入 finding.evidence 保证可解释。"""

    clause: str
    left: Any = None
    op: str = ""
    right: Any = None
    result: bool = False
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "clause": self.clause, "left": self._safe(self.left), "op": self.op,
            "right": self._safe(self.right), "result": self.result, "note": self.note,
        }

    @staticmethod
    def _safe(v: Any) -> Any:
        try:
            json.dumps(v)
            return v
        except (TypeError, ValueError):
            return str(v)


import json  # noqa: E402  （置于 TrailEntry 之后仅为可读性）


_CMP_OPS = {
    "gt": operator.gt, "gte": operator.ge,
    "lt": operator.lt, "lte": operator.le,
}
_ORDER_OPS = set(_CMP_OPS) | {"eq", "neq", "in", "not_in"}


def _to_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@dataclass
class RuleResult:
    rule_code: str
    hit: bool
    finding: FindingOut | None = None
    trail: list[dict] = field(default_factory=list)


class RuleEngine:
    """无状态求值器：每次调用传入 scope，线程安全。"""

    def __init__(self, functions: dict[str, Callable] | None = None):
        self.functions: dict[str, Callable] = functions or {}

    # ---------- operand 解析 ----------

    def resolve(self, operand: Any, scope: dict) -> Any:
        """解析 operand；变量缺失抛 MissingVariableError。"""
        if isinstance(operand, dict) and "fn" in operand:
            fn_name = operand["fn"]
            if fn_name not in self.functions:
                raise UnknownFunctionError(fn_name)
            args = [self.resolve(a, scope) for a in operand.get("args", [])]
            return self.functions[fn_name](*args)
        if isinstance(operand, str) and operand.startswith("$"):
            return self._lookup(operand[1:], scope)
        return operand

    def _resolve_or_missing(self, operand: Any, scope: dict) -> tuple[Any, bool]:
        try:
            return self.resolve(operand, scope), False
        except KeyError:
            return None, True

    @staticmethod
    def _lookup(path: str, scope: dict) -> Any:
        cur: Any = scope
        for seg in path.split("."):
            if isinstance(cur, dict) and seg in cur:
                cur = cur[seg]
            else:
                raise KeyError(path)
        return cur

    # ---------- 子句求值 ----------

    def eval_clause(self, clause: dict, scope: dict, trail: list[TrailEntry]) -> bool:
        op = clause.get("op", "")
        if op in ("and", "or"):
            results = [self.eval_clause(sub, scope, trail) for sub in clause.get("clauses", [])]
            res = all(results) if op == "and" else any(results)
            trail.append(TrailEntry(clause=op, result=res, note=f"{len(results)} sub-clauses"))
            return res

        if op == "not":
            res = not self.eval_clause(clause["clause"], scope, trail)
            trail.append(TrailEntry(clause="not", result=res))
            return res

        if op == "any_empty":
            paths = clause.get("left", [])
            paths = paths if isinstance(paths, list) else [paths]
            empties = []
            for p in paths:
                try:
                    v = self.resolve(p, scope)
                except KeyError:
                    v = None
                if v is None or v == "" or v == []:
                    empties.append(p)
            res = len(empties) > 0
            trail.append(TrailEntry(clause="any_empty", result=res,
                                    left=paths, note=f"empty: {empties}"))
            return res

        if op == "truthy":
            left, missing = self._resolve_or_missing(clause.get("left"), scope)
            res = bool(left) and not missing
            trail.append(TrailEntry(clause="truthy", result=res,
                                    left=left, note="variable missing" if missing else ""))
            return res

        if op in _ORDER_OPS:
            left, l_missing = self._resolve_or_missing(clause.get("left"), scope)
            right, r_missing = self._resolve_or_missing(clause.get("right"), scope)
            entry = TrailEntry(clause=op, left=left, right=right)
            if l_missing or r_missing:
                entry.result = False
                entry.note = "variable missing (fail-safe)"
            else:
                entry.result = self._compare(op, left, right)
            trail.append(entry)
            return entry.result

        raise ValueError(f"unknown clause op: {op!r}")

    @staticmethod
    def _compare(op: str, left: Any, right: Any) -> bool:
        if op == "eq":
            return left == right
        if op == "neq":
            return left != right
        if op == "in":
            return left in (right or [])
        if op == "not_in":
            return left not in (right or [])
        lf, rf = _to_float(left), _to_float(right)
        if lf is not None and rf is not None:
            return _CMP_OPS[op](lf, rf)
        if isinstance(left, str) and isinstance(right, str):
            return _CMP_OPS[op](left, right)
        return False

    # ---------- 规则级入口 ----------

    def evaluate_rule(self, dsl: dict, scope: dict) -> RuleResult:
        trail: list[TrailEntry] = []
        rule_code = dsl.get("rule_code", "?")
        hit = self.eval_clause(dsl.get("when", {}), scope, trail)
        result = RuleResult(rule_code=rule_code, hit=hit, trail=[t.to_dict() for t in trail])
        if not hit:
            return result

        then = dsl.get("then", {})
        message_args = [self.resolve(a, scope) for a in then.get("message_args", [])]
        try:
            message = then.get("message", "rule hit").format(*message_args)
        except (IndexError, KeyError):
            message = then.get("message", "rule hit")
        result.finding = FindingOut(
            problem_code=then.get("problem_code", "UNKNOWN"),
            severity=then.get("severity", "minor"),
            title=message,
            detail=f"规则 {rule_code} 命中；证据轨迹见 evidence。",
            engine="rule",
            rule_code=rule_code,
            evidence=result.trail,
        )
        return result

    def evaluate_rules(self, dsls: list[dict], scope: dict) -> list[RuleResult]:
        return [self.evaluate_rule(d, scope) for d in dsls]
