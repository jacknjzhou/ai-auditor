"""规则引擎 DSL 黄金用例（详细设计 §6 / §14）。"""
import pytest

from worker.pipeline.rules.engine import RuleEngine, UnknownFunctionError
from worker.pipeline.rules.functions import make_default_functions

TRAVEL_HOTEL_CAP = {
    "rule_code": "travel_hotel_cap",
    "when": {
        "op": "and",
        "clauses": [
            {"op": "eq", "left": "$form.type", "right": "差旅报销"},
            {"op": "gt", "left": "$attachment.ocr.avg_hotel_price",
             "right": "$std.hotel_cap"},
        ],
    },
    "then": {
        "problem_code": "OVER_STANDARD", "severity": "major",
        "message": "住宿单价 {0} 元/晚超过标准 {1} 元/晚",
        "message_args": ["$attachment.ocr.avg_hotel_price", "$std.hotel_cap"],
        "route_hint": "fin_manager",
    },
}


def base_scope(price=680, hotel_cap=500, trip_type="差旅报销"):
    return {
        "form": {"type": trip_type, "amount": 8600, "trip_reason": "客户支持"},
        "attachment": {"ocr": {"avg_hotel_price": price}},
        "ctx": {},
        "std": {"hotel_cap": hotel_cap},
    }


@pytest.fixture()
def engine():
    return RuleEngine(make_default_functions())


def test_hit_and_message_formatted(engine):
    res = engine.evaluate_rule(TRAVEL_HOTEL_CAP, base_scope(price=680))
    assert res.hit is True
    assert res.finding.problem_code == "OVER_STANDARD"
    assert res.finding.severity == "major"
    assert res.finding.title == "住宿单价 680 元/晚超过标准 500 元/晚"
    assert len(res.trail) == 3  # and + 2 子句，全部留痕


def test_not_hit_when_price_under_cap(engine):
    res = engine.evaluate_rule(TRAVEL_HOTEL_CAP, base_scope(price=480))
    assert res.hit is False
    assert res.finding is None


def test_not_hit_when_flow_type_differs(engine):
    res = engine.evaluate_rule(TRAVEL_HOTEL_CAP, base_scope(trip_type="采购申请"))
    assert res.hit is False


def test_missing_variable_is_fail_safe(engine):
    scope = base_scope()
    del scope["attachment"]["ocr"]["avg_hotel_price"]
    res = engine.evaluate_rule(TRAVEL_HOTEL_CAP, scope)
    assert res.hit is False
    missing_notes = [t for t in res.trail if "missing" in str(t.get("note", ""))]
    assert missing_notes, "缺失变量应记录在证据轨迹中"


def test_or_clause(engine):
    dsl = {"rule_code": "t", "when": {"op": "or", "clauses": [
        {"op": "eq", "left": "$form.type", "right": "采购申请"},
        {"op": "gt", "left": "$form.amount", "right": 8000},
    ]}, "then": {"problem_code": "X", "severity": "minor"}}
    res = engine.evaluate_rule(dsl, base_scope())
    assert res.hit is True


def test_any_empty_detects_missing_field(engine):
    dsl = {"rule_code": "t", "when": {"op": "any_empty",
                                      "left": ["$form.trip_reason", "$form.trip_range"]},
           "then": {"problem_code": "MISSING_FIELD", "severity": "minor"}}
    scope = base_scope()
    scope["form"]["trip_range"] = ""
    res = engine.evaluate_rule(dsl, scope)
    assert res.hit is True


def test_fn_call_and_nested_fn(engine):
    dsl = {"rule_code": "overdue", "when": {"op": "gt",
            "left": {"fn": "days_between", "args": ["$form.trip_end", "$ctx.today"]},
            "right": 90},
           "then": {"problem_code": "OVERDUE_SUBMIT", "severity": "minor"}}
    scope = base_scope()
    scope["form"]["trip_end"] = "2026-01-10"
    scope["ctx"]["today"] = "2026-09-28"
    res = engine.evaluate_rule(dsl, scope)
    assert res.hit is True


def test_fn_budget_balance_from_std(engine):
    dsl = {"rule_code": "budget", "when": {"op": "gt", "left": "$form.amount",
            "right": {"fn": "budget_balance",
                      "args": ["$form.dept", "$form.subject", "$ctx.quarter"]}},
           "then": {"problem_code": "OVER_BUDGET", "severity": "major"}}
    scope = base_scope()
    scope["form"].update(dept="研发中心", subject="设备采购")
    scope["ctx"]["quarter"] = "2026Q3"
    scope["std"]["budget"] = {"研发中心|设备采购|2026Q3": 31200.0}
    res = engine.evaluate_rule(dsl, scope)
    assert res.hit is True


def test_unknown_function_raises(engine):
    dsl = {"rule_code": "t", "when": {"op": "truthy",
           "left": {"fn": "no_such_fn"}}, "then": {}}
    with pytest.raises(UnknownFunctionError):
        engine.evaluate_rule(dsl, base_scope())


def test_in_operator(engine):
    dsl = {"rule_code": "t", "when": {"op": "in",
           "left": "$form.type", "right": ["差旅报销", "采购申请"]},
           "then": {}}
    assert engine.evaluate_rule(dsl, base_scope()).hit is True
