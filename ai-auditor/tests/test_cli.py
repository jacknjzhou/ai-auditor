"""CLI 测试 —— 容器 init 服务与裸机部署同源的运维入口。

覆盖：SOP 装载（幂等 / 保留运营档位 / --force 覆盖）、演示种子幂等、
数据层自检、`main()` 子命令端到端。使用 conftest 的内存 SQLite 夹具。
"""
from __future__ import annotations

from pathlib import Path

from app.cli import check, load_sops, main, seed_demo
from app.models.entities import FlowProfile, PolicyRule, SessionLocal

SOP_DIR = Path(__file__).resolve().parent.parent / "config" / "sops"


def _profile(flow_code: str) -> FlowProfile | None:
    db = SessionLocal()
    try:
        return db.get(FlowProfile, flow_code)
    finally:
        db.close()


def _set_flow(flow_code: str, **fields) -> None:
    db = SessionLocal()
    try:
        p = db.get(FlowProfile, flow_code)
        for k, v in fields.items():
            setattr(p, k, v)
        db.commit()
    finally:
        db.close()


def test_load_sops_installs_seal_application(client):
    """装载 config/sops → flow_profile 出现 seal_application（SOP + 路由表）。"""
    flows = load_sops(SOP_DIR)
    assert "seal_application" in flows

    p = _profile("seal_application")
    assert p is not None
    assert p.audit_sop["entry"] == "intake"
    assert len(p.audit_sop["nodes"]) == 6
    assert p.mode == "advisory"
    assert p.writeback_tier == "COMMENT_ONLY"
    assert len(p.route_table) == 3  # .routes.json 一并装载
    assert any("SEAL_UNAUTHORIZED" in r["problem_codes"] for r in p.route_table)


def test_load_sops_is_idempotent(client):
    """重复装载不产生重复 flow（upsert 语义）。"""
    load_sops(SOP_DIR)
    load_sops(SOP_DIR)
    db = SessionLocal()
    try:
        rows = (db.query(FlowProfile)
                .filter_by(flow_code="seal_application").all())
    finally:
        db.close()
    assert len(rows) == 1


def test_load_sops_preserves_existing_mode(client):
    """已存在 flow 的运营档位不被重复装载覆盖（默认 force=False）。"""
    load_sops(SOP_DIR)
    _set_flow("seal_application", mode="full_auto", writeback_tier="FULL_API")

    load_sops(SOP_DIR)  # 再装一次（如容器重启）

    p = _profile("seal_application")
    assert p.mode == "full_auto"
    assert p.writeback_tier == "FULL_API"
    assert p.audit_sop["entry"] == "intake"  # SOP 仍刷新


def test_load_sops_force_overrides(client):
    """--force 时以配置文件默认值覆盖已存在档位。"""
    load_sops(SOP_DIR)
    _set_flow("seal_application", mode="full_auto")

    load_sops(SOP_DIR, force=True)

    assert _profile("seal_application").mode == "advisory"


def test_load_sops_missing_dir_raises(client):
    import pytest

    with pytest.raises(FileNotFoundError):
        load_sops(SOP_DIR / "not-exists")


def test_seed_demo_is_idempotent(client):
    """演示规则只种一次（第二次跳过，不触发 policy_rule 唯一约束冲突）。"""
    assert seed_demo("expense") is True
    assert seed_demo("expense") is False

    db = SessionLocal()
    try:
        n = db.query(PolicyRule).filter_by(flow_code="expense").count()
    finally:
        db.close()
    assert n == 2


def test_check_reports_datastores(client, monkeypatch):
    """自检覆盖 PG(SELECT 1) 与 Redis(PING) 两端。"""
    import redis

    class _FakeRedis:
        def ping(self) -> bool:
            return True

        def close(self) -> None:
            pass

    monkeypatch.setattr(redis.Redis, "from_url",
                        classmethod(lambda cls, *a, **k: _FakeRedis()))
    out = check()
    assert out == {"postgres": "ok", "redis": "ok"}


def test_main_init_seed_and_check(client, monkeypatch, capsys):
    """端到端：python -m app.cli init --seed → 建表 + 装载 SOP + 种子。"""
    rc = main(["init", "--seed", "--sops-dir", str(SOP_DIR)])
    assert rc == 0
    assert "装载 SOP" in capsys.readouterr().out

    assert _profile("seal_application") is not None
    db = SessionLocal()
    try:
        # --seed 为装载的 flow 种入演示规则
        assert db.query(PolicyRule).filter_by(flow_code="seal_application").count() == 2
    finally:
        db.close()


def test_main_load_sops_and_seed_commands(client, capsys):
    assert main(["load-sops", "--sops-dir", str(SOP_DIR)]) == 0
    assert main(["seed", "expense"]) == 0
    assert main(["seed", "expense"]) == 0  # 第二次跳过但仍成功退出
    out = capsys.readouterr().out
    assert "已装载 1 个 flow" in out
    assert "已存在规则，跳过" in out
