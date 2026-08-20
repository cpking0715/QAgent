"""飞书事件适配器：收文件建任务、收文本走对话，复用同一 Service。"""

from __future__ import annotations

import http.client
import json
import logging
import os
import threading
import time
from typing import Any

from qagent.server.service import QAgentService

logger = logging.getLogger("qagent.server.feishu")

# ── 飞书 API 常量 ──────────────────────────────────────────
_FEISHU_HOST = "open.feishu.cn"
_TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
_SEND_PATH = "/open-apis/im/v1/messages?receive_id_type=chat_id"
_DOWNLOAD_TPL = "/open-apis/im/v1/messages/{mid}/resources/{fk}?type=file"

_TOKEN_TTL_SECONDS = 90 * 60  # 飞书 token 有效期约 2 小时，提前刷新
_token_lock = threading.Lock()
_token_cache: list = [0.0, ""]  # [获取时间(monotonic), token]


def _https_post(host: str, path: str, body: dict, headers: dict | None = None) -> dict:
    """通过 HTTPS 直连飞书 API，不经过 urllib 避免 SSRF 风险。"""
    conn = http.client.HTTPSConnection(host, timeout=20)
    try:
        payload = json.dumps(body).encode("utf-8")
        hdrs = {"Content-Type": "application/json", "Content-Length": str(len(payload))}
        if headers:
            hdrs.update(headers)
        conn.request("POST", path, body=payload, headers=hdrs)
        resp = conn.getresponse()
        return json.loads(resp.read().decode("utf-8"))
    finally:
        conn.close()


def _https_get(host: str, path: str, headers: dict | None = None) -> bytes:
    """通过 HTTPS GET 下载飞书资源。"""
    conn = http.client.HTTPSConnection(host, timeout=60)
    try:
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        return resp.read()
    finally:
        conn.close()


def _tenant_token() -> str:
    app_id = os.environ.get("FEISHU_APP_ID", "")
    secret = os.environ.get("FEISHU_APP_SECRET", "")
    if not app_id or not secret:
        return ""
    body = _https_post(_FEISHU_HOST, _TOKEN_PATH, {"app_id": app_id, "app_secret": secret})
    return str(body.get("tenant_access_token") or "")


def _cached_tenant_token() -> str:
    """带缓存的 tenant token：TTL 内复用，避免每条消息都重新获取。"""
    now = time.monotonic()
    if _token_cache[1] and now - _token_cache[0] < _TOKEN_TTL_SECONDS:
        return _token_cache[1]
    with _token_lock:
        if _token_cache[1] and time.monotonic() - _token_cache[0] < _TOKEN_TTL_SECONDS:
            return _token_cache[1]
        token = _tenant_token()
        if token:
            _token_cache[0] = time.monotonic()
            _token_cache[1] = token
        return token


def reply_text(chat_id: str, text: str) -> None:
    token = _cached_tenant_token()
    if not token or not chat_id:
        return
    payload = {
        "receive_id": chat_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }
    try:
        _https_post(_FEISHU_HOST, _SEND_PATH, payload, {"Authorization": f"Bearer {token}"})
    except Exception as exc:
        logger.warning("飞书回复失败 chat=%s error=%s", chat_id, exc)


def download_message_file(message_id: str, file_key: str) -> bytes:
    token = _cached_tenant_token()
    if not token:
        raise RuntimeError("未配置 FEISHU_APP_ID / FEISHU_APP_SECRET，无法下载飞书文件")
    path = _DOWNLOAD_TPL.format(mid=message_id, fk=file_key)
    return _https_get(_FEISHU_HOST, path, {"Authorization": f"Bearer {token}"})


def handle_feishu_event(
    service: QAgentService,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    expected = os.environ.get("FEISHU_VERIFICATION_TOKEN", "").strip()
    token = str(payload.get("token") or payload.get("header", {}).get("token") or "")
    if expected and token and token != expected:
        return {"error": "verification token mismatch"}

    if payload.get("type") == "url_verification" or payload.get("challenge"):
        return {"challenge": payload.get("challenge", "")}

    header = payload.get("header") or {}
    event_type = header.get("event_type") or payload.get("event_type")
    if event_type != "im.message.receive_v1":
        return {"ok": True, "ignored": event_type}

    event = payload.get("event") or {}
    message = event.get("message") or {}
    sender = event.get("sender") or {}
    chat_id = str(message.get("chat_id") or "")
    user_id = str(
        (sender.get("sender_id") or {}).get("open_id")
        or (sender.get("sender_id") or {}).get("user_id")
        or "feishu",
    )
    msg_type = str(message.get("message_type") or "text")
    content_raw = message.get("content") or "{}"
    try:
        content = json.loads(content_raw) if isinstance(content_raw, str) else content_raw
    except json.JSONDecodeError:
        content = {"text": str(content_raw)}

    if msg_type in {"file", "media"} or content.get("file_key"):
        file_key = str(content.get("file_key") or "")
        file_name = str(content.get("file_name") or "upload.bin")
        message_id = str(message.get("message_id") or "")
        data = download_message_file(message_id, file_key)
        job = service.create_job(user_id, [(file_name, data)], title=file_name)
        service.store.bind_feishu(job["id"], chat_id, user_id)
        if job.get("awaiting_scope"):
            draft = ""
            chat = job.get("chat") or []
            if chat:
                draft = str(chat[-1].get("content") or "")
            reply_text(chat_id, f"已创建任务 {job['id']}。\n{draft}")
        else:
            service.start_run(job["id"], "requirements")
            reply_text(chat_id, f"已创建任务 {job['id']}，正在按你提供的测试需求生成。")
        return {"ok": True, "job_id": job["id"]}

    text = str(content.get("text") or "").strip()
    if not text:
        return {"ok": True}
    job_id = service.store.job_for_feishu_chat(chat_id)
    if not job_id:
        reply_text(chat_id, "请先发送 PRD/设计文档，我会据此生成测试方案。")
        return {"ok": True, "need_file": True}
    result = service.chat(job_id, text)
    reply_text(chat_id, str(result.get("reply") or "已处理"))
    return {"ok": True, "job_id": job_id, "chat": result.get("ok")}
