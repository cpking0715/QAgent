"""飞书事件适配器：收文件建任务、收文本走对话，复用同一 Service。"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from qagent.server.service import QAgentService


def _tenant_token() -> str:
    app_id = os.environ.get("FEISHU_APP_ID", "")
    secret = os.environ.get("FEISHU_APP_SECRET", "")
    if not app_id or not secret:
        return ""
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": app_id, "app_secret": secret}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return str(body.get("tenant_access_token") or "")


def reply_text(chat_id: str, text: str) -> None:
    token = _tenant_token()
    if not token or not chat_id:
        return
    payload = {
        "receive_id": chat_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=20)
    except urllib.error.HTTPError:
        return


def download_message_file(message_id: str, file_key: str) -> bytes:
    token = _tenant_token()
    if not token:
        raise RuntimeError("未配置 FEISHU_APP_ID / FEISHU_APP_SECRET，无法下载飞书文件")
    url = (
        f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{file_key}"
        "?type=file"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


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
        service.start_run(job["id"], "requirements")
        reply_text(chat_id, f"已创建任务 {job['id']}，正在生成测试方案和用例。")
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
