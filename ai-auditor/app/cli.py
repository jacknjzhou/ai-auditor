"""ai-auditor 运维 CLI —— 容器编排（compose 的一次性 init 服务）与裸机部署共用。

命令：
    python -m app.cli init [--seed] [--force]  建表 + 装载 config/sops/*.yaml（幂等）
    python -m app.cli load-sops                仅装载 config/sops/（幂等 upsert flow_profile）
    python -m app.cli seed <flow_code>         种入演示规则（policy_rule，幂等）
    python -m app.cli check                    数据层连通自检（PostgreSQL + Redis）

设计要点：
- **幂等**：容器首次建库、重启、滚动升级均可反复执行，不产生重复数据；
- **不覆盖运营配置**：重复装载只刷新 audit_sop / route_table，保留 flow 现有的
  mode / writeback_tier（可用 --force 强制以配置文件为准）；
- **与容器解耦**：裸机 `python -m app.cli init` 与 compose 的 init 服务同源同实现。

SOP 装载目录默认 `<repo>/config/sops`，可用环境变量 `AUDITOR_SOPS_DIR` 覆盖；
新建 flow 的默认 mode / writeback_tier 由 `AUDITOR_SOP_MODE` /
`AUDITOR_SOP_WRITEBACK_TIER` 决定（默认 advisory / COMMENT_ONLY）。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger("app.cli")


def _default_sops_dir() -> Path:
    """默认 SOP 目录：<repo>/config/sops（app/cli.py 的上两级）。"""
    env = os.getenv("AUDITOR_SOPS_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "config" / "sops"


def load_sops(sops_dir: str | Path | None = None, *,
              force: bool = False) -> list[str]:
    """装载目录下全部 SOP 到 flow_profile（幂等）；返回已装载的 flow_code 列表。

    - flow_code 取 SOP 文件名（不含扩展名），同目录同名 `.routes.json` 为路由表；
    - 已存在的 flow 默认**保留**其 mode / writeback_tier（运营侧可能已调整），
      仅刷新 SOP 与路由表；`force=True` 时以配置文件默认值覆盖；
    - 装载期静态校验失败（坏 YAML / 非法转移）立即抛错，坏配置不进运行库。
    """
    from app.models.entities import FlowProfile, SessionLocal
    from worker.pipeline.kernel.sop_registry import install_flow_sop

    d = Path(sops_dir) if sops_dir else _default_sops_dir()
    if not d.is_dir():
        raise FileNotFoundError(f"SOP 目录不存在: {d}")

    files = sorted(d.glob("*.yaml")) + sorted(d.glob("*.yml"))
    loaded: list[str] = []
    db = SessionLocal()
    try:
        for sop_file in files:
            if sop_file.name.startswith("_") or sop_file.name.startswith("."):
                continue
            flow_code = sop_file.stem
            routes = sop_file.with_suffix(".routes.json")
            existing = db.get(FlowProfile, flow_code)
            # 已存在的 flow 保留运营侧已调整的 mode / tier（除非 --force）
            mode = (os.getenv("AUDITOR_SOP_MODE", "advisory")
                    if existing is None or force else existing.mode)
            tier = (os.getenv("AUDITOR_SOP_WRITEBACK_TIER", "COMMENT_ONLY")
                    if existing is None or force else existing.writeback_tier)
            install_flow_sop(
                db, flow_code, sop_path=sop_file,
                route_table_path=routes if routes.exists() else None,
                mode=mode, writeback_tier=tier)
            db.commit()
            loaded.append(flow_code)
            logger.info("SOP 已装载: flow=%s file=%s routes=%s",
                        flow_code, sop_file.name,
                        routes.name if routes.exists() else "-")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return loaded


def seed_demo(flow_code: str) -> bool:
    """种入演示规则（幂等）：该 flow 已有规则则跳过，返回是否实际种入。"""
    from app.models.entities import PolicyRule, SessionLocal
    from app.services.audit_service import seed_demo_rules

    db = SessionLocal()
    try:
        if db.query(PolicyRule).filter_by(flow_code=flow_code).count() > 0:
            logger.info("flow=%s 已有规则，跳过种子", flow_code)
            return False
        seed_demo_rules(db, flow_code)
        logger.info("flow=%s 演示规则已种入", flow_code)
        return True
    finally:
        db.close()


def check() -> dict[str, str]:
    """数据层连通自检：PostgreSQL SELECT 1 + Redis PING。失败即抛错（非零退出）。"""
    from sqlalchemy import text

    from app.config import settings
    from app.models.entities import engine

    out: dict[str, str] = {}
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    out["postgres"] = "ok"

    import redis  # arq 的依赖，无需额外安装

    client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=3,
                                  socket_timeout=3)
    try:
        client.ping()
        out["redis"] = "ok"
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001 —— 老版本 redis-py 无 close
            pass
    return out


def _cmd_init(args: argparse.Namespace) -> int:
    from app.models.entities import init_db

    # 建表（幂等）：生产库通常已由 sql/ddl.sql 初始化，此处为兜底（SQLite/测试）
    init_db()
    flows = load_sops(args.sops_dir, force=args.force)
    print(f"[init] 建表完成；装载 SOP {len(flows)} 个: {', '.join(flows) or '-'}")
    if args.seed:
        for fc in (args.seed_flow or flows):
            if seed_demo(fc):
                print(f"[init] 演示规则已种入: {fc}")
    return 0


def _cmd_load_sops(args: argparse.Namespace) -> int:
    flows = load_sops(args.sops_dir, force=args.force)
    print(f"[load-sops] 已装载 {len(flows)} 个 flow: {', '.join(flows) or '-'}")
    return 0


def _cmd_seed(args: argparse.Namespace) -> int:
    seeded = seed_demo(args.flow_code)
    print(f"[seed] flow={args.flow_code} "
          f"{'已种入演示规则' if seeded else '已存在规则，跳过'}")
    return 0


def _cmd_check(_: argparse.Namespace) -> int:
    result = check()
    print("[check] " + " ".join(f"{k}={v}" for k, v in result.items()))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli", description="ai-auditor 运维 CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="建表 + 装载 SOP（compose init 服务入口）")
    p_init.add_argument("--seed", action="store_true",
                        help="额外为装载的 flow 种入演示规则（缺规则时）")
    p_init.add_argument("--seed-flow", nargs="*", default=None,
                        help="指定种子的 flow_code（默认全部已装载 flow）")
    p_init.add_argument("--force", action="store_true",
                        help="以配置文件默认值覆盖已存在 flow 的 mode/tier")
    p_init.add_argument("--sops-dir", default=None, help="SOP 目录（默认 config/sops）")
    p_init.set_defaults(func=_cmd_init)

    p_load = sub.add_parser("load-sops", help="仅装载 config/sops/（幂等）")
    p_load.add_argument("--force", action="store_true")
    p_load.add_argument("--sops-dir", default=None)
    p_load.set_defaults(func=_cmd_load_sops)

    p_seed = sub.add_parser("seed", help="种入演示规则（幂等）")
    p_seed.add_argument("flow_code")
    p_seed.set_defaults(func=_cmd_seed)

    p_check = sub.add_parser("check", help="数据层连通自检")
    p_check.set_defaults(func=_cmd_check)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
