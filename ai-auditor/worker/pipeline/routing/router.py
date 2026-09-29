"""转人工路由引擎 —— 对应详细设计 §7（配置化路由表）。

输入：FlowProfile.route_table + 决策级别 + findings 问题码
输出：RoutePlan（target_node + action + comment），shadow 模式恒为 None。

路由表条目（flow_profile.route_table，JSON）：
    {"problem_codes": ["MISSING_RECEIPT"], "target_node": "applicant",
     "action": "RETURN", "priority": 1}
匹配规则：按问题码交集命中，priority 小者优先；未命中走 default_target。

动作语义：
    RETURN   打回指定节点（默认打回提单人）
    ESCALATE 加签/转办到目标节点
    COMMENT  仅附加意见，单据继续流转
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.models.entities import FlowProfile
from app.schemas.canonical import FindingOut

ACTION_DEFAULT_NODE = {
    "RETURN": "applicant",
    "ESCALATE": "fin_manager",
    "COMMENT": "",
}


@dataclass
class RoutePlan:
    action: str
    target_node: str
    problem_codes: list[str] = field(default_factory=list)
    comment: str = ""
    sample_check: bool = False  # 半自动模式随机抽检标记


def match_route(profile: FlowProfile, problem_codes: list[str]) -> tuple[str, str] | None:
    """路由表匹配：返回 (action, target_node)；未命中 None。"""
    best: tuple[int, str, str] | None = None
    for item in profile.route_table or []:
        hit = set(item.get("problem_codes", [])) & set(problem_codes)
        if not hit:
            continue
        prio = int(item.get("priority", 99))
        cand = (prio, str(item.get("action", "COMMENT")),
                str(item.get("target_node") or ACTION_DEFAULT_NODE.get(item.get("action", ""), "")))
        if best is None or cand[0] < best[0]:
            best = cand
    if best is None:
        return None
    return best[1], best[2]


def build_comment(decision_level: str, findings: list[FindingOut],
                  summary: str = "") -> str:
    """生成回写意见文本（数字人署名）。"""
    if not findings and decision_level in ("PASS", "ADVISORY"):
        text = "【数字人审核】材料齐全、规则通过，未发现问题。"
    else:
        lines = [f"- {f.problem_code}（{f.severity}）：{f.title} {f.detail}".rstrip()
                 for f in findings]
        text = "【数字人审核】发现以下问题：\n" + "\n".join(lines)
    if summary:
        text += f"\n模型评估：{summary}"
    return text


def route(profile: FlowProfile, decision_level: str, findings: list[FindingOut],
          *, summary: str = "") -> RoutePlan | None:
    """决策 → 处置计划。shadow 返回 None（只审不动，记录由调用方处理）。"""
    if profile.mode == "shadow":
        return None

    problem_codes = [f.problem_code for f in findings]

    if decision_level == "REJECT":
        matched = match_route(profile, problem_codes)
        action, node = matched if matched else ("RETURN", "applicant")
        return RoutePlan(action=action, target_node=node,
                         problem_codes=problem_codes,
                         comment=build_comment(decision_level, findings, summary))

    if decision_level == "ESCALATE":
        matched = match_route(profile, problem_codes)
        action, node = matched if matched else ("ESCALATE", "fin_manager")
        return RoutePlan(action=action, target_node=node,
                         problem_codes=problem_codes,
                         comment=build_comment(decision_level, findings, summary))

    if decision_level == "ADVISORY":
        if not problem_codes:
            return None  # 高置信建议通过 → 附意见但不打扰人工
        return RoutePlan(action="COMMENT", target_node="",
                         problem_codes=problem_codes,
                         comment=build_comment(decision_level, findings, summary))

    if decision_level == "PASS" and profile.sample_rate > 0:
        import random
        if random.random() < profile.sample_rate:
            return RoutePlan(action="SAMPLE_CHECK", target_node="",
                             problem_codes=[], comment="自动通过，随机抽检",
                             sample_check=True)
    return None
