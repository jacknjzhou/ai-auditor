"""Webhook 入站 API 用例：HMAC 签名 / 防重放 / 幂等 / 端到端流水线。"""
import time


def _payload(amount=50000, reason="客户支持", iid="AP202609280012", version=0):
    return {
        "source_system": "oa-a8",
        "event_type": "NODE_ARRIVED",
        "version": version,
        "instance": {
            "instance_id": iid,
            "flow_code": "expense_reimburse",
            "current_node": "fin_review",
            "form": {"amount": amount, "trip_reason": reason, "type": "差旅报销"},
            "applicant": {"user_id": "u1", "name": "张三"},
        },
    }


def test_rejects_missing_signature(client):
    r = client.post("/api/v1/webhooks/oa-a8", json=_payload())
    assert r.status_code == 401


def test_rejects_bad_signature(client, sign):
    raw, headers = sign(_payload(), secret="wrong-secret")
    r = client.post("/api/v1/webhooks/oa-a8", content=raw, headers=headers)
    assert r.status_code == 401


def test_rejects_stale_timestamp(client, sign):
    raw, headers = sign(_payload(), timestamp=int(time.time()) - 3600)
    r = client.post("/api/v1/webhooks/oa-a8", content=raw, headers=headers)
    assert r.status_code == 401


def test_accept_and_clean_flow_advisory(client, sign):
    raw, headers = sign(_payload())
    r = client.post("/api/v1/webhooks/oa-a8", content=raw, headers=headers)
    assert r.status_code == 202
    body = r.json()
    assert body["deduplicated"] is False
    detail = client.get(f"/api/v1/audit-tasks/{body['task_id']}").json()
    assert detail["status"] == "decided"
    assert detail["decision"]["level"] == "ADVISORY"
    assert detail["findings"] == []


def test_hard_rule_rejects_big_amount(client, sign):
    raw, headers = sign(_payload(amount=500000))
    r = client.post("/api/v1/webhooks/oa-a8", content=raw, headers=headers)
    detail = client.get(f"/api/v1/audit-tasks/{r.json()['task_id']}").json()
    assert detail["decision"]["level"] == "REJECT"
    assert any(f["problem_code"] == "HIGH_AMOUNT" for f in detail["findings"])


def test_minor_finding_advisory(client, sign):
    raw, headers = sign(_payload(reason=""))
    r = client.post("/api/v1/webhooks/oa-a8", content=raw, headers=headers)
    detail = client.get(f"/api/v1/audit-tasks/{r.json()['task_id']}").json()
    assert detail["decision"]["level"] == "ADVISORY"
    assert any(f["problem_code"] == "MISSING_FIELD" for f in detail["findings"])


def test_idempotent_replay_returns_same_task(client, sign):
    raw, headers = sign(_payload())
    first = client.post("/api/v1/webhooks/oa-a8", content=raw, headers=headers).json()
    second = client.post("/api/v1/webhooks/oa-a8", content=raw, headers=headers).json()
    assert first["deduplicated"] is False
    assert second["deduplicated"] is True
    assert second["task_id"] == first["task_id"]


def test_different_version_creates_new_task(client, sign):
    raw, headers = sign(_payload(version=0))
    first = client.post("/api/v1/webhooks/oa-a8", content=raw, headers=headers).json()
    raw2, headers2 = sign(_payload(version=1))
    second = client.post("/api/v1/webhooks/oa-a8", content=raw2, headers=headers2).json()
    assert second["deduplicated"] is False
    assert second["task_id"] != first["task_id"]


def test_invalid_json_rejected(client, sign):
    raw, headers = sign(b"{not json")
    r = client.post("/api/v1/webhooks/oa-a8", content=raw, headers=headers)
    assert r.status_code == 400
