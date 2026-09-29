"""决策矩阵黄金用例（详细设计 §4.4 / §11）。"""
from app.schemas.canonical import FindingOut
from worker.pipeline.fusion.matrix import fuse


def f(code="HIGH_AMOUNT", sev="major"):
    return FindingOut(problem_code=code, severity=sev, title="", detail="")


def test_hard_rule_violation_rejects():
    level, reason = fuse([f("OVER_BUDGET", "major")], llm_confidence=0.95,
                         auto_allowed=True)
    assert level == "REJECT"
    assert reason["cell"] == "rule_hard_violation"


def test_clean_and_auto_allowed_passes():
    level, reason = fuse([], llm_confidence=None, auto_allowed=True)
    assert level == "PASS"
    assert reason["cell"] == "rule_pass_no_llm_auto_allowed"


def test_clean_but_not_auto_allowed_advisory():
    level, _ = fuse([], llm_confidence=None, auto_allowed=False)
    assert level == "ADVISORY"


def test_minor_findings_never_auto_pass():
    level, _ = fuse([f("MISSING_FIELD", "minor")], llm_confidence=None,
                    auto_allowed=True)
    assert level == "ADVISORY"


def test_llm_high_confidence_with_auto():
    level, _ = fuse([], llm_confidence=0.93, auto_allowed=True)
    assert level == "PASS"


def test_llm_high_confidence_without_auto():
    level, _ = fuse([], llm_confidence=0.93, auto_allowed=False)
    assert level == "ADVISORY"


def test_llm_mid_confidence_advisory():
    level, reason = fuse([], llm_confidence=0.75, auto_allowed=True)
    assert level == "ADVISORY"
    assert reason["cell"] == "rule_pass_llm_mid"


def test_llm_low_confidence_advisory():
    level, reason = fuse([], llm_confidence=0.4, auto_allowed=True)
    assert level == "ADVISORY"
    assert reason["cell"] == "rule_pass_llm_low"
