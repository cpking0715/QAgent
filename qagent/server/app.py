"""QAgent HTTP 服务：任务 API + Web + 飞书回调。"""

from __future__ import annotations

import cgi
import json
import mimetypes
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from qagent.ingest import SUPPORTED
from qagent.server.auth import authorize
from qagent.server.feishu import handle_feishu_event
from qagent.server.jobs import JobStore, default_jobs_root
from qagent.server.service import QAgentService

STATIC_DIR = Path(__file__).resolve().parent / "static"


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


def _multipart_fields(form: cgi.FieldStorage, *names: str) -> list:
    """cgi.FieldStorage.getlist() 返回的是文件内容而非字段对象，不能用来取 filename。"""
    wanted = set(names)
    items = []
    for item in form.list or []:
        if getattr(item, "name", None) in wanted:
            items.append(item)
    return items


def _uploads_from_form(form: cgi.FieldStorage) -> list[tuple[str, bytes]]:
    uploads: list[tuple[str, bytes]] = []
    for item in _multipart_fields(form, "files", "file"):
        filename = getattr(item, "filename", None) or ""
        name = Path(str(filename)).name
        if not name or Path(name).suffix.lower() not in SUPPORTED:
            continue
        if getattr(item, "file", None):
            data = item.file.read()
        else:
            value = item.value
            data = value if isinstance(value, bytes) else str(value).encode("utf-8")
        if data:
            uploads.append((name, data))
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
                page = (STATIC_DIR / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
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
            if path == "/api/jobs":
                _json(self, 200, {"jobs": service.list_jobs(None)})
                return
            parts = path.split("/")
            if len(parts) == 4 and parts[1:3] == ["api", "jobs"]:
                try:
                    _json(self, 200, service.get_job(parts[3]))
                except FileNotFoundError as exc:
                    _json(self, 404, {"error": str(exc)})
                return
            if len(parts) == 6 and parts[1:3] == ["api", "jobs"] and parts[4] == "artifacts":
                try:
                    file_path = service.artifact_path(parts[3], parts[5])
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
            if path == "/api/jobs":
                self._create_job(owner)
                return
            parts = path.split("/")
            if len(parts) == 5 and parts[1:3] == ["api", "jobs"] and parts[4] == "run":
                try:
                    body = _read_json(self)
                    _json(self, 200, service.start_run(parts[3], str(body.get("from") or "requirements")))
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

        def _create_job(self, owner: str) -> None:
            content_type = self.headers.get("Content-Type", "")
            uploads: list[tuple[str, bytes]] = []
            if "multipart/form-data" in content_type:
                form = cgi.FieldStorage(
                    fp=self.rfile,
                    headers=self.headers,
                    environ={
                        "REQUEST_METHOD": "POST",
                        "CONTENT_TYPE": content_type,
                        "CONTENT_LENGTH": self.headers.get("Content-Length") or "0",
                    },
                )
                uploads = _uploads_from_form(form)
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
