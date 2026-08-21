"""QAgent HTTP 服务：任务 API + Web + SSE 推送。"""

from __future__ import annotations

import json
import logging
import mimetypes
import re
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from qagent.config import public_llm_settings, update_local_llm
from qagent.ingest import SUPPORTED
from qagent.server.auth import authorize, configured_token
from qagent.server.feishu import handle_feishu_event
from qagent.server.jobs import JobStore, default_jobs_root
from qagent.server.service import QAgentService

logger = logging.getLogger("qagent.server.app")

STATIC_DIR = Path(__file__).resolve().parent / "static"
_INDEX_CACHE: bytes | None = None
SSE_INTERVAL_SECONDS = 1.0
_TERMINAL_STATUSES = {"ready", "failed", "cancelled", "uploaded"}


def _index_html() -> bytes:
    global _INDEX_CACHE
    if _INDEX_CACHE is not None:
        return _INDEX_CACHE
    candidates = [
        Path.cwd() / "qagent" / "server" / "static" / "index.html",
        STATIC_DIR / "index.html",
    ]
    for path in candidates:
        if path.is_file():
            _INDEX_CACHE = path.read_bytes()
            return _INDEX_CACHE
    raise FileNotFoundError("缺少 qagent/server/static/index.html，请在仓库根目录运行 qagent serve")


def _json(handler: BaseHTTPRequestHandler, code: int, payload: dict) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _headers(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    return {k: v for k, v in handler.headers.items()}


def _read_json(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length") or 0)
    raw = handler.rfile.read(length) if length else b"{}"
    if not raw:
        return {}
    data = json.loads(raw.decode("utf-8"))
    return data if isinstance(data, dict) else {}


def _multipart_boundary(content_type: str) -> str:
    match = re.search(r'boundary="?([^";]+)"?', content_type)
    return match.group(1) if match else ""


def _parse_multipart(body: bytes, boundary: str) -> list[dict]:
    """解析 multipart/form-data（替代 cgi.FieldStorage，兼容 Python 3.12+）。

    返回 [{"name", "filename"|"", "data"}]，解析失败返回空列表。
    """
    if not boundary:
        return []
    delim = b"--" + boundary.encode("utf-8")
    fields: list[dict] = []
    for section in body.split(delim)[1:]:
        if section[:2] == b"--":  # 结束边界
            break
        section = section.lstrip(b"\r\n")
        head, sep, content = section.partition(b"\r\n\r\n")
        if not sep:
            continue
        if content.endswith(b"\r\n"):
            content = content[:-2]
        headers_text = head.decode("utf-8", errors="replace")
        name = ""
        filename = ""
        disposition = ""
        for line in headers_text.splitlines():
            lower = line.lower()
            if lower.startswith("content-disposition:"):
                disposition = line
        name_match = re.search(r'name="([^"]*)"', disposition)
        file_match = re.search(r'filename="([^"]*)"', disposition)
        if name_match:
            name = name_match.group(1)
        if file_match:
            filename = file_match.group(1)
        fields.append({"name": name, "filename": filename, "data": content})
    return fields


def _uploads_from_multipart(body: bytes, content_type: str) -> list[tuple[str, bytes]]:
    uploads: list[tuple[str, bytes]] = []
    for field in _parse_multipart(body, _multipart_boundary(content_type)):
        if field["name"] not in {"files", "file"}:
            continue
        name = Path(field["filename"] or "").name
        if not name or Path(name).suffix.lower() not in SUPPORTED:
            continue
        if field["data"]:
            uploads.append((name, field["data"]))
    return uploads


def create_handler(service: QAgentService):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def _auth(self) -> str | None:
            ok, owner = authorize(_headers(self))
            if not ok:
                _json(self, 401, {"error": "未授权"})
                return None
            return owner

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            if path == "/":
                page = _index_html()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
                return
            if path == "/health":
                _json(self, 200, {"ok": True})
                return
            owner = self._auth()
            if owner is None:
                return
            if path == "/api/settings":
                _json(self, 200, public_llm_settings())
                return
            if path == "/api/jobs":
                _json(self, 200, {"jobs": service.list_jobs(
                    owner if owner != "anonymous" else None,
                )})
                return
            parts = path.split("/")
            if len(parts) == 4 and parts[1:3] == ["api", "jobs"]:
                try:
                    _json(self, 200, service.get_job(parts[3]))
                except FileNotFoundError as exc:
                    _json(self, 404, {"error": str(exc)})
                return
            if len(parts) == 5 and parts[1:3] == ["api", "jobs"] and parts[4] == "events":
                self._sse_events(parts[3], parsed.query)
                return
            if len(parts) == 6 and parts[1:3] == ["api", "jobs"] and parts[4] == "inputs":
                # GET /api/jobs/{id}/inputs/{name} —— 输入文档预览（pdf/docx 抽取文本）
                try:
                    text = service.input_file_text(parts[3], unquote(parts[5]))
                except FileNotFoundError as exc:
                    _json(self, 404, {"error": str(exc)})
                    return
                except ValueError as exc:
                    _json(self, 400, {"error": str(exc)})
                    return
                data = text.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if len(parts) == 5 and parts[1:3] == ["api", "jobs"] and parts[4] == "open-with":
                # GET /api/jobs/{id}/open-with?target=&name= —— 枚举本机打开方式
                try:
                    query = parse_qs(parsed.query)
                    _json(self, 200, service.list_open_with(
                        parts[3],
                        (query.get("target") or ["artifact"])[0],
                        unquote((query.get("name") or [""])[0]),
                    ))
                except FileNotFoundError as exc:
                    _json(self, 404, {"error": str(exc)})
                except (RuntimeError, ValueError) as exc:
                    _json(self, 400, {"error": str(exc)})
                return
            if len(parts) == 6 and parts[1:3] == ["api", "jobs"] and parts[4] == "artifacts":
                try:
                    file_path = service.artifact_path(parts[3], unquote(parts[5]))
                except (FileNotFoundError, ValueError) as exc:
                    _json(self, 404, {"error": str(exc)})
                    return
                data = file_path.read_bytes()
                mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Disposition", f'attachment; filename="{file_path.name}"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_error(404)

        def do_POST(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            if path == "/api/feishu/event":
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    payload = json.loads(body.decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    _json(self, 400, {"error": "invalid json"})
                    return
                _json(self, 200, handle_feishu_event(service, payload, _headers(self)))
                return
            owner = self._auth()
            if owner is None:
                return
            if path == "/api/settings":
                body = _read_json(self)
                try:
                    _json(
                        self,
                        200,
                        update_local_llm(
                            api_key=body.get("api_key"),
                            model=body.get("model"),
                            base_url=body.get("base_url"),
                        ),
                    )
                except ValueError as exc:
                    _json(self, 400, {"error": str(exc)})
                return
            if path == "/api/jobs":
                self._create_job(owner)
                return
            parts = path.split("/")
            if len(parts) == 5 and parts[1:3] == ["api", "jobs"] and parts[4] == "run":
                try:
                    body = _read_json(self)
                    stop_after = body.get("stop_after") or None
                    if stop_after is not None and not isinstance(stop_after, str):
                        stop_after = None
                    _json(self, 200, service.start_run(
                        parts[3], str(body.get("from") or "requirements"), stop_after,
                    ))
                except (FileNotFoundError, RuntimeError, ValueError) as exc:
                    _json(self, 400, {"error": str(exc)})
                return
            if len(parts) == 5 and parts[1:3] == ["api", "jobs"] and parts[4] == "cancel":
                try:
                    _json(self, 200, service.cancel_job(parts[3]))
                except (FileNotFoundError, RuntimeError, ValueError) as exc:
                    _json(self, 400, {"error": str(exc)})
                return
            if len(parts) == 5 and parts[1:3] == ["api", "jobs"] and parts[4] == "chat":
                try:
                    body = _read_json(self)
                    _json(self, 200, service.start_chat(parts[3], str(body.get("message") or "")))
                except (FileNotFoundError, RuntimeError, ValueError) as exc:
                    _json(self, 400, {"error": str(exc)})
                return
            if len(parts) == 5 and parts[1:3] == ["api", "jobs"] and parts[4] == "open":
                # POST /api/jobs/{id}/open {target, name, app?} —— 本地应用打开
                try:
                    body = _read_json(self)
                    app = body.get("app")
                    _json(self, 200, service.open_file(
                        parts[3],
                        str(body.get("target") or "artifact"),
                        str(body.get("name") or ""),
                        app=str(app) if isinstance(app, str) and app else None,
                    ))
                except FileNotFoundError as exc:
                    _json(self, 404, {"error": str(exc)})
                except (RuntimeError, ValueError) as exc:
                    _json(self, 400, {"error": str(exc)})
                return
            if len(parts) == 5 and parts[1:3] == ["api", "jobs"] and parts[4] == "review":
                # POST /api/jobs/{id}/review {target, name} —— AI 审阅文件
                try:
                    body = _read_json(self)
                    _json(self, 200, service.start_review(
                        parts[3],
                        str(body.get("target") or "artifact"),
                        str(body.get("name") or ""),
                    ))
                except FileNotFoundError as exc:
                    _json(self, 404, {"error": str(exc)})
                except (RuntimeError, ValueError) as exc:
                    _json(self, 400, {"error": str(exc)})
                return
            self.send_error(404)

        def do_PUT(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            owner = self._auth()
            if owner is None:
                return
            parts = path.split("/")
            # PUT /api/jobs/{id}/artifacts/{name} —— 人工修改产物（分阶段确认工作流）
            if len(parts) == 6 and parts[1:3] == ["api", "jobs"] and parts[4] == "artifacts":
                try:
                    body = _read_json(self)
                    _json(self, 200, service.save_artifact(
                        parts[3], parts[5], str(body.get("content") or ""),
                    ))
                except FileNotFoundError as exc:
                    _json(self, 404, {"error": str(exc)})
                except (ValueError, RuntimeError) as exc:
                    _json(self, 400, {"error": str(exc)})
                return
            self.send_error(404)

        def do_DELETE(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            owner = self._auth()
            if owner is None:
                return
            parts = path.split("/")
            if len(parts) == 4 and parts[1:3] == ["api", "jobs"]:
                try:
                    service.delete_job(parts[3])
                except FileNotFoundError as exc:
                    _json(self, 404, {"error": str(exc)})
                    return
                except RuntimeError as exc:
                    _json(self, 400, {"error": str(exc)})
                    return
                _json(self, 200, {"ok": True, "id": parts[3]})
                return
            self.send_error(404)

        def _sse_events(self, job_id: str, query: str) -> None:
            """SSE 推送任务状态/日志变化；EventSource 无法带 header，token 走 query。"""
            expected = configured_token()
            if expected:
                token = (parse_qs(query).get("token") or [""])[0]
                if token != expected:
                    _json(self, 401, {"error": "未授权"})
                    return
            try:
                job = service.get_job(job_id)
            except FileNotFoundError as exc:
                _json(self, 404, {"error": str(exc)})
                return
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                last = ""
                while True:
                    job = service.get_job(job_id)
                    snapshot = json.dumps(job, ensure_ascii=False, sort_keys=True)
                    if snapshot != last:
                        last = snapshot
                        self.wfile.write(f"data: {snapshot}\n\n".encode("utf-8"))
                        self.wfile.flush()
                    if job.get("status") in _TERMINAL_STATUSES:
                        break
                    time.sleep(SSE_INTERVAL_SECONDS)
            except (BrokenPipeError, ConnectionResetError):
                return  # 客户端断开
            except Exception:
                logger.exception("SSE 推送异常 job=%s", job_id)

        def _create_job(self, owner: str) -> None:
            content_type = self.headers.get("Content-Type", "")
            uploads: list[tuple[str, bytes]] = []
            if "multipart/form-data" in content_type:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                uploads = _uploads_from_multipart(body, content_type)
            if not uploads:
                _json(self, 400, {"error": "请上传 md/pdf/docx 文档"})
                return
            job = service.create_job(owner, uploads)
            _json(self, 200, job)

    return Handler


def serve(
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    store: JobStore | None = None,
    service: QAgentService | None = None,
) -> None:
    job_store = store or JobStore(default_jobs_root())
    app = service or QAgentService(job_store)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    stale = job_store.mark_stale_on_startup()
    if stale:
        logger.warning("服务重启：%d 个中断任务已标记为 failed（可续跑）", stale)
    httpd = ThreadingHTTPServer((host, port), create_handler(app))
    url = f"http://{host}:{port}/"
    print(f"[QAgent] 服务已启动 {url}  任务目录 {job_store.root}", flush=True)
    if open_browser and host in {"127.0.0.1", "localhost"}:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[QAgent] 已停止")
    finally:
        httpd.server_close()
