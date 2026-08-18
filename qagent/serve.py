"""QAgent Web 上传界面：上传文档 → 生成测试方案与用例。"""

from __future__ import annotations

import cgi
import html
import json
import mimetypes
import re
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

from qagent.agent.llm import OpenAILLM
from qagent.agent.runner import QAgentRunner
from qagent.config import resolve_config
from qagent.ingest import SUPPORTED, collect_documents, ingest

STEPS = [
    ("ingest", "摄入文档"),
    ("2", "生成测试需求"),
    ("3", "生成测试方案"),
    ("4", "生成风险分析"),
    ("5", "生成测试用例"),
    ("6", "校验与修正"),
    ("7", "导出 Excel"),
]

_run_state: dict = {
    "running": False,
    "step": "",
    "message": "就绪",
    "logs": [],
    "started_at": None,
    "result": None,
    "error": None,
}
_run_lock = threading.Lock()


def _uploads_dir(config) -> Path:
    return config.input_dir / "uploads"


def _compiled_path(config) -> Path:
    return config.input_dir / "uploads" / "_compiled" / "requirement.md"


def _parse_step(message: str) -> str:
    m = re.search(r"Step (\d)/7", message)
    if m:
        return m.group(1)
    if "已合并" in message or "已摄入" in message or "摄入" in message:
        return "ingest"
    if "完成" in message and "用例" in message:
        return "done"
    return ""


def _append_log(message: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    step = _parse_step(message)
    with _run_lock:
        _run_state["logs"].append(f"[{ts}] {message}")
        _run_state["logs"] = _run_state["logs"][-50:]
        _run_state["message"] = message
        if step:
            _run_state["step"] = step


def _status_payload() -> dict:
    with _run_lock:
        data = dict(_run_state)
    started = data.get("started_at")
    if data.get("running") and started:
        data["elapsed_seconds"] = int(time.time() - started)
    else:
        data["elapsed_seconds"] = 0
    data["steps"] = STEPS
    return data


def _run_agent(config) -> None:
    global _run_state
    try:
        _append_log("开始摄入上传文档 ...")
        uploads = _uploads_dir(config)
        compiled = _compiled_path(config)
        result = ingest(uploads, compiled, workspace=config.workspace)
        _append_log(f"已合并 {len(result.product_paths)} 份产品文档")
        if result.test_requirements_text:
            _append_log("已加载测试需求（将优先遵循测试范围与重点）")

        llm = OpenAILLM(config.llm)
        runner = QAgentRunner(config, llm, on_log=_append_log)
        result = runner.run(compiled)
        with _run_lock:
            if result.success:
                _run_state.update({
                    "running": False,
                    "step": "done",
                    "message": f"完成：{result.case_count} 条用例",
                    "result": {k: path.name for k, path in result.artifacts.items()},
                    "error": None,
                })
            else:
                _run_state.update({
                    "running": False,
                    "step": "error",
                    "message": "生成失败",
                    "result": None,
                    "error": result.errors,
                })
    except Exception as exc:
        _append_log(f"ERROR: {exc}")
        with _run_lock:
            _run_state.update({
                "running": False,
                "step": "error",
                "message": "生成失败",
                "result": None,
                "error": [str(exc)],
            })


def _page(config, notice: str = "") -> bytes:
    uploads = _uploads_dir(config)
    uploads.mkdir(parents=True, exist_ok=True)
    try:
        files = collect_documents(uploads)
        file_list = "".join(
            f"<li>{html.escape(p.name)} ({p.stat().st_size // 1024} KB)</li>"
            for p in files
        )
    except FileNotFoundError:
        file_list = "<li><em>暂无文档，请上传</em></li>"

    exts = ", ".join(sorted(SUPPORTED))
    status = _status_payload()
    running = status["running"]

    body = f"""<!DOCTYPE html>
<html lang="zh"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>QAgent</title>
<style>
  body {{ font-family: -apple-system, sans-serif; max-width: 760px; margin: 32px auto; padding: 0 16px; color: #1e293b; }}
  h1 {{ font-size: 1.4rem; margin-bottom: 4px; }}
  .sub {{ color: #64748b; font-size: 0.9rem; margin-bottom: 20px; }}
  .card {{ border: 1px solid #e2e8f0; border-radius: 10px; padding: 18px 20px; margin: 14px 0; background: #fff; }}
  .notice {{ color: #b45309; background: #fffbeb; padding: 8px 12px; border-radius: 6px; margin-bottom: 12px; }}
  button {{ background: #2563eb; color: white; border: none; padding: 10px 18px; border-radius: 8px; cursor: pointer; font-size: 0.95rem; }}
  button:disabled {{ background: #94a3b8; cursor: not-allowed; }}
  input[type=file] {{ margin: 10px 0; width: 100%; }}
  .steps {{ list-style: none; padding: 0; margin: 12px 0; }}
  .steps li {{ padding: 8px 10px; margin: 4px 0; border-radius: 6px; font-size: 0.9rem; color: #64748b; }}
  .steps li.active {{ background: #eff6ff; color: #1d4ed8; font-weight: 600; }}
  .steps li.done {{ color: #15803d; }}
  .steps li.done::before {{ content: "✓ "; }}
  .progress-bar {{ height: 6px; background: #e2e8f0; border-radius: 3px; overflow: hidden; margin: 10px 0; }}
  .progress-bar div {{ height: 100%; background: #2563eb; width: 0%; transition: width 0.4s; }}
  .meta {{ font-size: 0.85rem; color: #64748b; }}
  .log {{ background: #0f172a; color: #e2e8f0; font-family: ui-monospace, monospace; font-size: 0.75rem; padding: 12px; border-radius: 8px; max-height: 220px; overflow-y: auto; white-space: pre-wrap; }}
  .result a {{ color: #2563eb; }}
  .err {{ color: #b91c1c; }}
  #progress-panel {{ display: none; }}
  #progress-panel.show {{ display: block; }}
</style></head><body>
<h1>QAgent</h1>
<p class="sub">上传 PRD / 设计文档 → 测试需求 → 测试方案 → 测试用例</p>
{"<p class='notice'>" + html.escape(notice) + "</p>" if notice else ""}

<div class="card">
  <h2>1. 上传文档</h2>
  <p>支持：<code>{html.escape(exts)}</code></p>
  <p class="meta">建议同时上传 <strong>测试需求.md</strong>（或在工作区放置 <code>input/test-requirements.md</code>），
  用于指定测试范围、API 重点、环境数据，可显著提升用例质量。模板见 <code>templates/test-requirements.example.md</code></p>
  <form method="POST" action="/upload" enctype="multipart/form-data">
    <input type="file" name="files" multiple required>
    <br><button type="submit" id="upload-btn">上传</button>
  </form>
  <h3>已上传</h3>
  <ul id="file-list">{file_list}</ul>
</div>

<div class="card">
  <h2>2. 生成</h2>
  <button id="run-btn" {"disabled" if running else ""} onclick="startRun()">开始生成（需求→方案→用例）</button>
  <p class="meta">大文档约需 7–15 分钟（含测试需求阶段），LLM 步骤会较慢，下方会实时刷新进度。</p>
</div>

<div class="card" id="progress-panel">
  <h2>进度</h2>
  <div class="progress-bar"><div id="progress-fill"></div></div>
  <p class="meta" id="elapsed">已用时 0 秒</p>
  <ul class="steps" id="step-list">
    {"".join(f'<li data-step="{sid}">{label}</li>' for sid, label in STEPS)}
  </ul>
  <p id="status-msg" class="meta">{html.escape(status.get("message", ""))}</p>
  <div class="log" id="log-box"></div>
  <div id="result-box" class="result"></div>
</div>

<script>
const STEP_ORDER = {json.dumps([s[0] for s in STEPS])};
let pollTimer = null;

function stepIndex(step) {{
  if (step === 'done') return STEP_ORDER.length;
  const i = STEP_ORDER.indexOf(step);
  return i >= 0 ? i : 0;
}}

function renderStatus(s) {{
  const panel = document.getElementById('progress-panel');
  const runBtn = document.getElementById('run-btn');
  panel.classList.add('show');
  runBtn.disabled = s.running;

  const idx = stepIndex(s.step);
  const pct = Math.min(100, Math.round((idx / STEP_ORDER.length) * 100));
  document.getElementById('progress-fill').style.width = pct + '%';
  document.getElementById('elapsed').textContent =
    '已用时 ' + (s.elapsed_seconds || 0) + ' 秒' + (s.running ? '（运行中，LLM 调用期间可能 1-3 分钟无新日志，属正常）' : '');

  document.querySelectorAll('#step-list li').forEach(li => {{
    const sid = li.dataset.step;
    const si = STEP_ORDER.indexOf(sid);
    li.classList.remove('active', 'done');
    if (si < idx) li.classList.add('done');
    else if (si === idx && s.running) li.classList.add('active');
    else if (s.step === 'done') li.classList.add('done');
  }});

  document.getElementById('status-msg').textContent = s.message || '';
  document.getElementById('log-box').textContent = (s.logs || []).join('\\n');

  const resultBox = document.getElementById('result-box');
  resultBox.innerHTML = '';
  if (s.result) {{
    resultBox.innerHTML = '<p style="color:#15803d"><strong>' + s.message + '</strong></p><ul>' +
      Object.entries(s.result).map(([k,v]) =>
        '<li><a href="/output/' + v + '" target="_blank">' + v + '</a></li>').join('') + '</ul>';
  }} else if (s.error && s.error.length) {{
    resultBox.innerHTML = '<p class="err"><strong>失败</strong></p><ul>' +
      s.error.map(e => '<li class="err">' + e + '</li>').join('') + '</ul>';
  }}

  if (!s.running && pollTimer) {{
    clearInterval(pollTimer);
    pollTimer = null;
  }}
}}

async function pollStatus() {{
  try {{
    const r = await fetch('/status');
    renderStatus(await r.json());
  }} catch (e) {{ console.error(e); }}
}}

async function startRun() {{
  document.getElementById('run-btn').disabled = true;
  document.getElementById('progress-panel').classList.add('show');
  document.getElementById('log-box').textContent = '正在启动...';
  await fetch('/run', {{ method: 'POST' }});
  pollStatus();
  if (!pollTimer) pollTimer = setInterval(pollStatus, 2000);
}}

if ({json.dumps(running)}) {{
  document.getElementById('progress-panel').classList.add('show');
  pollStatus();
  pollTimer = setInterval(pollStatus, 2000);
}}
</script>
</body></html>"""
    return body.encode("utf-8")


def create_handler(config):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            print(f"[QAgent Web] {args[0]}")

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(_page(config))
                return
            if parsed.path.startswith("/output/"):
                name = parsed.path.removeprefix("/output/")
                path = config.output_dir / name
                if path.is_file() and path.parent.resolve() == config.output_dir.resolve():
                    data = path.read_bytes()
                    ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Disposition", f'inline; filename="{path.name}"')
                    self.end_headers()
                    self.wfile.write(data)
                    return
            if parsed.path == "/status":
                payload = json.dumps(_status_payload(), ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_error(404)

        def do_POST(self):
            parsed = urlparse(self.path)
            if parsed.path == "/upload":
                self._handle_upload()
            elif parsed.path == "/run":
                self._handle_run()
            else:
                self.send_error(404)

        def _handle_upload(self):
            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in content_type:
                self.send_error(400, "需要 multipart/form-data")
                return
            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": content_type},
            )
            uploads = _uploads_dir(config)
            uploads.mkdir(parents=True, exist_ok=True)
            saved = 0
            items = form["files"] if isinstance(form.get("files"), list) else (
                [form["files"]] if form.get("files") else []
            )
            for item in items:
                if not item.filename:
                    continue
                name = Path(item.filename).name
                if Path(name).suffix.lower() not in SUPPORTED:
                    continue
                (uploads / name).write_bytes(item.file.read())
                saved += 1
            notice = f"已上传 {saved} 个文件" if saved else "未上传有效文件"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_page(config, notice=notice))

        def _handle_run(self):
            global _run_state
            with _run_lock:
                if _run_state["running"]:
                    payload = json.dumps({"ok": False, "message": "已有任务在运行"}).encode("utf-8")
                else:
                    _run_state = {
                        "running": True,
                        "step": "ingest",
                        "message": "启动中 ...",
                        "logs": [],
                        "started_at": time.time(),
                        "result": None,
                        "error": None,
                    }
                    thread = threading.Thread(target=_run_agent, args=(config,), daemon=True)
                    thread.start()
                    payload = json.dumps({"ok": True}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    config = resolve_config()
    _uploads_dir(config).mkdir(parents=True, exist_ok=True)
    config.output_dir.mkdir(parents=True, exist_ok=True)
    handler = create_handler(config)
    server = HTTPServer((host, port), handler)
    url = f"http://{host}:{port}/"
    print(f"[QAgent] Web 上传界面: {url}")
    print(f"[QAgent] 文档目录: {_uploads_dir(config)}")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[QAgent] 已停止")
