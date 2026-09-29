"""内部标准单据模型（Canonical Model）。

所有接入适配器的输出、所有审核引擎的输入统一为此模型，
对应详细设计文档 §3。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class Party(BaseModel):
    user_id: str
    name: str
    dept_path: str = ""


class Attachment(BaseModel):
    file_id: str
    file_type: Literal["invoice", "contract", "receipt", "other"] = "other"
    uri: str
    ocr_text: Optional[str] = None
    ocr_struct: Optional[dict[str, Any]] = None


class NodeAction(BaseModel):
    node: str
    action: Literal["APPROVE", "REJECT", "COMMENT"]
    comment: str = ""
    actor: str = ""
    time: Optional[datetime] = None


class CanonicalForm(BaseModel):
    fields: dict[str, Any] = Field(default_factory=dict)
    attachments: list[Attachment] = Field(default_factory=list)


class CanonicalApproval(BaseModel):
    """标准单据快照 —— 预处理完成后冻结写入 audit_task.snapshot。"""

    source_code: str
    instance_id: str
    flow_code: str
    node_code: str
    event_type: Literal["NODE_ARRIVED", "SUBMITTED", "NODE_PASSED"]
    version_no: int = 0
    submitted_at: Optional[datetime] = None
    applicant: Party
    form: CanonicalForm
    history: list[NodeAction] = Field(default_factory=list)

    def field(self, name: str, default: Any = None) -> Any:
        return self.form.fields.get(name, default)


class AuditContext(BaseModel):
    """规则引擎上下文：$ctx.* 与 $std.* 的来源。"""

    ctx: dict[str, Any] = Field(default_factory=dict)
    std: dict[str, Any] = Field(default_factory=dict)


class FindingOut(BaseModel):
    """规则引擎产出的单条问题（写入 audit_finding 前的形态）。"""

    problem_code: str
    severity: Literal["info", "minor", "major", "critical"]
    title: str
    detail: str
    engine: str = "rule"
    rule_code: Optional[str] = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)
