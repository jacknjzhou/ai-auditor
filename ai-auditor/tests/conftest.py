"""pytest 全局夹具。

测试使用内存 SQLite（StaticPool 共享连接），不依赖 PostgreSQL。
"""
import hmac
import hashlib
import json
import os
import time

os.environ["AUDITOR_DATABASE_URL"] = "sqlite://"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.models.entities import Base, engine, init_db  # noqa: E402


@pytest.fixture()
def client():
    Base.metadata.drop_all(engine)
    init_db()
    with TestClient(app) as c:
        yield c
    Base.metadata.drop_all(engine)


@pytest.fixture()
def sign():
    """构造合法 HMAC 签名头。secret 与 AUDITOR_WEBHOOK_SECRET_DEFAULT 默认值一致。"""

    def _sign(payload: dict | bytes, secret: str = "dev-secret",
              timestamp: int | None = None) -> tuple[bytes, dict]:
        raw = payload if isinstance(payload, (bytes, bytearray)) \
            else json.dumps(payload).encode()
        ts = str(timestamp if timestamp is not None else int(time.time()))
        sig = hmac.new(secret.encode(), ts.encode() + b"." + raw,
                       hashlib.sha256).hexdigest()
        return raw, {"X-Auditor-Signature": "sha256=" + sig,
                     "X-Auditor-Timestamp": ts}

    return _sign
