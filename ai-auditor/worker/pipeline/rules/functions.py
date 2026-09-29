"""规则引擎内置函数库（DSL 中以 {"fn": "...", "args": [...]} 调用）。

设计约束（详细设计 §6.1）：
- 内置函数在求值时懒加载并缓存（预算余额等外部查询由 provider 注入）；
- 函数只做「取数/计算」，不做业务决策 —— 决策仍由规则命中 + 决策矩阵完成。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Callable


def _parse_date(v: Any) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def make_default_functions(std: dict | None = None) -> dict[str, Callable]:
    """构建默认函数表。

    std: 外部标准值（差旅标准、黑名单等），生产中由 std_provider 服务注入；
         此处直接以字典传入，便于测试与本地运行。
    """
    std = std or {}

    def days_between(start: Any, end: Any) -> int:
        """end - start 的天数（可用于报销超期等时效规则）。"""
        return (_parse_date(end) - _parse_date(start)).days

    def budget_balance(dept: str, subject: str, quarter: str) -> float:
        """部门-科目-季度预算余额。键约定：'{dept}|{subject}|{quarter}'。"""
        budgets = std.get("budget", {})
        return float(budgets.get(f"{dept}|{subject}|{quarter}", 0.0))

    def duplicate_fingerprint(form: dict | None = None) -> bool:
        """同人同金额近 7 天重复提交检测（P0 为桩，P1 接查重指纹索引）。"""
        return bool(std.get("duplicate_hit", False))

    def user_in_role(user_id: str, role_code: str) -> bool:
        """权限/角色校验（P0 为桩）。"""
        roles = std.get("user_roles", {}).get(str(user_id), [])
        return role_code in roles

    def seal_count_today(user_id: str) -> int:
        """当日用章申请次数（P0 为桩）。"""
        return int(std.get("seal_count_today", 0))

    return {
        "days_between": days_between,
        "budget_balance": budget_balance,
        "duplicate_fingerprint": duplicate_fingerprint,
        "user_in_role": user_in_role,
        "seal_count_today": seal_count_today,
    }
