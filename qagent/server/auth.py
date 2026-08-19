"""内网共享 Token。未配置则开发模式放行。"""

from __future__ import annotations

import os


def configured_token() -> str:
    return os.environ.get("QAGENT_TOKEN", "").strip()


def owner_from_headers(headers: dict[str, str]) -> str:
    return (
        headers.get("X-User")
        or headers.get("X-Feishu-User")
        or "anonymous"
    ).strip() or "anonymous"


def authorize(headers: dict[str, str]) -> tuple[bool, str]:
    expected = configured_token()
    if not expected:
        return True, owner_from_headers(headers)
    auth = headers.get("Authorization") or headers.get("authorization") or ""
    token = ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    elif headers.get("X-QAgent-Token"):
        token = headers["X-QAgent-Token"].strip()
    if token != expected:
        return False, ""
    return True, owner_from_headers(headers)
