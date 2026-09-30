"""外部审批系统对接自测工具 —— 两个角色，一键验证双向协议。

  publish  扮演**审批系统**：向 ai-auditor 推送一条带正确 HMAC 签名的
           Canonical 事件，并轮询任务直到出决策，打印 findings / decision。
  listen   扮演**审批系统侧的回写端点**：接收 ai-auditor 下发的处置回写指令
           （tier / action / target_node / comment），原样打印供比对。

用法（对接联调时开两个终端）：

  # 终端 1：起回写端点，模拟 OA 侧接收处置
  python tools/onboard_check.py listen --port 9100

  # 终端 2：推一条大额差旅单（触发 hard violation → REJECT → 回写打回）
  python tools/onboard_check.py publish --url http://localhost:8300 \\
      --source oa-a8 --secret dev-secret --flow expense_reimburse --amount 500000

  # 干净单（无 finding，看 decisions 与 writeback 的差别）
  python tools/onboard_check.py publish --amount 2380 --trip-reason "客户支持"

仅依赖标准库（urllib / http.server），可在任何机器上直接运行。
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
import time
import urllib.error
import urllib.request
import uuid

REPLAY_WINDOW_NOTE = "签名 = hmac_sha256(secret, f\"{timestamp}.{raw_body}\")"


# ---------------------------------------------------------------- publish
def build_payload(args: argparse.Namespace) -> dict:
    """构造 Canonical 事件（字段语义见 app/schemas/canonical.py）。"""
    return {
        "source_system": args.source,
        "event_type": "SUBMITTED",          # NODE_ARRIVED | SUBMITTED | NODE_PASSED
        "version": args.version,
        "instance": {
            "instance_id": args.instance_id or f"IT-{uuid.uuid4().hex[:8]}",
            "flow_code": args.flow,
            "current_node": args.node,
            "submitted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "applicant": {"user_id": "u1001", "name": "张三",
                          "dept_path": "总部/财务部"},
            "form": {
                "amount": args.amount,
                "trip_reason": args.trip_reason,
                "budget_code": "B-2026-Q3",
            },
            "attachments": [{
                "file_id": "f-001", "file_type": "invoice",
                "uri": "https://oa.example.com/files/f-001.pdf",
                "ocr_text": "住宿费 1200 元",
                "ocr_struct": {"total": 1200, "avg_hotel_price": 600},
            }],
            # ctx / std：外部系统补充的扩展上下文，供规则 DSL 的 $ctx.* / $std.* 使用
            "ctx": {"submit_channel": "web", "dept_code": "FIN-01"},
            "std": {"hotel_cap": 500, "is_key_project": False},
        },
    }


def sign(raw: bytes, secret: str, ts: int) -> dict[str, str]:
    """构造 HMAC-SHA256 签名头（与 app/api/v1/webhooks.py::verify_signature 对齐）。"""
    sig = hmac.new(secret.encode(), str(ts).encode() + b"." + raw,
                   hashlib.sha256).hexdigest()
    return {"X-Auditor-Signature": "sha256=" + sig,
            "X-Auditor-Timestamp": str(ts),
            "Content-Type": "application/json"}


def post_json(url: str, raw: bytes, headers: dict[str, str]) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=raw, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw": body}


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def cmd_publish(args: argparse.Namespace) -> int:
    payload = build_payload(args)
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ts = int(time.time())
    headers = sign(raw, args.secret, ts)

    url = f"{args.url.rstrip('/')}/api/v1/webhooks/{args.source}"
    print(f"→ POST {url}")
    print(f"  payload: {json.dumps(payload, ensure_ascii=False)[:300]}...")
    status, body = post_json(url, raw, headers)
    print(f"← HTTP {status} {json.dumps(body, ensure_ascii=False)}")
    if status != 202:
        print("✗ 入站失败：核对 source_code / 密钥 / 时间戳 / payload 结构")
        return 1

    task_id = body.get("task_id")
    if not task_id:
        return 1
    if body.get("deduplicated"):
        print("  （幂等命中：该事件此前已受理，返回原 task_id）")

    # 轮询直到出决策
    deadline = time.time() + args.wait
    last = None
    while time.time() < deadline:
        try:
            last = get_json(f"{args.url.rstrip('/')}/api/v1/audit-tasks/{task_id}")
        except urllib.error.HTTPError as exc:
            print(f"  查询失败 HTTP {exc.code}")
            return 1
        if last.get("status") == "decided":
            break
        time.sleep(0.5)

    print(f"\n□ task {task_id}")
    print(f"  status   : {last.get('status')}")
    print(f"  findings : {json.dumps(last.get('findings'), ensure_ascii=False)}")
    print(f"  decision : {json.dumps(last.get('decision'), ensure_ascii=False)}")
    if last.get("status") != "decided":
        print("  （未在等待窗口内出决策：检查 worker 是否在跑 / queue_mode / 日志）")
    return 0


# ---------------------------------------------------------------- listen
def cmd_listen(args: argparse.Namespace) -> int:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n)
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:  # noqa: BLE001
                data = {"raw": raw.decode("utf-8", "replace")}
            print(f"\n← 收到回写 {self.path}")
            print("  " + json.dumps(data, ensure_ascii=False, indent=2).replace(
                "\n", "\n  "))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

        def log_message(self, *a) -> None:  # 静默默认访问日志
            pass

    print(f"回写端点已监听 http://0.0.0.0:{args.port}（Ctrl-C 退出）")
    print(f"把 flow_profile.writeback_config.http_endpoint 指向 "
          f"http://<本机可达地址>:{args.port}/writeback")
    print(f"提示：ai-auditor 跑在容器内时用 host.docker.internal:{args.port}")
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="外部审批系统对接自测工具")
    sub = p.add_subparsers(dest="cmd", required=True)

    pub = sub.add_parser("publish", help="推送一条 Canonical 事件并跟踪结果")
    pub.add_argument("--url", default="http://localhost:8300")
    pub.add_argument("--source", default="oa-a8", help="source_code（webhook 路径段）")
    pub.add_argument("--secret", default="dev-secret",
                     help="connector_config.webhook_secret（缺省回退全局默认）")
    pub.add_argument("--flow", default="expense_reimburse", help="flow_code")
    pub.add_argument("--node", default="fin_review", help="current_node")
    pub.add_argument("--amount", type=float, default=2380.0)
    pub.add_argument("--trip-reason", default="客户支持")
    pub.add_argument("--instance-id", default=None, help="不传则随机生成")
    pub.add_argument("--version", type=int, default=0)
    pub.add_argument("--wait", type=float, default=20.0, help="等待决策秒数")
    pub.set_defaults(func=cmd_publish)

    lis = sub.add_parser("listen", help="起一个回写端点接收处置指令")
    lis.add_argument("--port", type=int, default=9100)
    lis.set_defaults(func=cmd_listen)
    return p


def main(argv: list[str] | None = None) -> int:
    # 行缓冲：listen 常被重定向/task runner 拉起，块缓冲会让调试输出「看不见」
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):  # 老版本 / 非标准流
        pass
    args = build_parser().parse_args(argv)
    print(f"[签名算法] {REPLAY_WINDOW_NOTE}")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
