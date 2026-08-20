"""阶段 4 回归：multipart 解析、SSE、流式取消、池分离、stale 标记、read 回流。"""

from __future__ import annotations

import io
import json
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

from qagent.agent.llm import LLMCancelled, OpenAILLM
from qagent.config import LLMConfig
from qagent.server.app import _multipart_boundary, _parse_multipart
from qagent.server.jobs import JobStore
from qagent.server.service import QAgentService
from fixtures_loader import mock_responses  # noqa: F401  (fixture)

FIXTURES = Path(__file__).parent / "fixtures"


# ---- multipart 解析（替代 cgi） ----

def _multipart_body(boundary: str, *parts: tuple[str, str, bytes]) -> bytes:
    chunks = []
    for name, filename, data in parts:
        disposition = f'form-data; name="{name}"'
        if filename:
            disposition += f'; filename="{filename}"'
        chunks.append(
            f"--{boundary}\r\nContent-Disposition: {disposition}\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n".encode("utf-8")
            + data + b"\r\n"
        )
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks)


def test_multipart_parser_files_and_fields():
    boundary = "----QAgentBoundary"
    body = _multipart_body(
        boundary,
        ("files", "prd.md", "# 需求\n".encode("utf-8")),
        ("files", "设计.md", "# 设计\n".encode("utf-8")),
        ("note", "", "普通字段".encode("utf-8")),
    )
    assert _multipart_boundary(f"multipart/form-data; boundary={boundary}") == boundary
    fields = _parse_multipart(body, boundary)
    files = [f for f in fields if f["filename"]]
    assert len(files) == 2
    assert files[0]["filename"] == "prd.md" and files[0]["data"] == "# 需求\n".encode("utf-8")
    note = [f for f in fields if not f["filename"]]
    assert note[0]["name"] == "note" and note[0]["data"].decode("utf-8") == "普通字段"


def test_multipart_parser_binary_safe():
    boundary = "B"
    blob = bytes(range(256))
    body = _multipart_body(boundary, ("files", "bin.md", blob))
    fields = _parse_multipart(body, boundary)
    assert fields[0]["data"] == blob


# ---- 流式 LLM：聚合与取消 ----

def _stream_response(lines: list[bytes]):
    class _Resp(io.BytesIO):
        def __iter__(self):
            return iter(lines)
    return _Resp()


def _sse_chunks(*contents: str) -> list[bytes]:
    out = []
    for c in contents:
        out.append(b"data: " + json.dumps(
            {"choices": [{"delta": {"content": c}}]}
        ).encode("utf-8") + b"\n")
    out.append(b"data: [DONE]\n")
    return out


def test_llm_stream_aggregates_content(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(1)
        return _stream_response(_sse_chunks("你好", "，", "世界"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setenv("OPENAI_API_KEY", "qagent-test-stub")
    llm = OpenAILLM(LLMConfig(retries=0, backoff_seconds=0, stream=True))
    assert llm.complete("s", "u") == "你好，世界"
    assert len(calls) == 1


def test_llm_stream_cancel_between_chunks(monkeypatch):
    def fake_urlopen(request, timeout):
        return _stream_response(_sse_chunks("部分1", "部分2", "部分3"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setenv("OPENAI_API_KEY", "qagent-test-stub")
    llm = OpenAILLM(LLMConfig(retries=0, backoff_seconds=0, stream=True))

    # 第 3 次检查起取消：前两个 chunk 正常读到，随后中断
    checks = {"n": 0}

    def cancel_after_two() -> bool:
        checks["n"] += 1
        return checks["n"] > 2

    llm.should_cancel = cancel_after_two
    with pytest.raises(LLMCancelled):
        llm.complete("s", "u")
    assert checks["n"] == 3  # 确实发生在 chunk 之间

    # 首个 chunk 前取消
    llm.should_cancel = lambda: True
    with pytest.raises(LLMCancelled):
        llm.complete("s", "u")


# ---- chat 池与 pipeline 池分离 ----

def test_chat_not_blocked_when_pipeline_pool_saturated(tmp_path, mock_responses):
    store = JobStore(tmp_path / "jobs")
    started = threading.Event()
    release = threading.Event()

    class GateLLM:
        def complete(self, system, user):
            started.set()
            release.wait(timeout=5)
            return (FIXTURES / "testcases-valid.md").read_text(encoding="utf-8")

    service = QAgentService(store, llm_factory=lambda: GateLLM(), max_pipeline=1)
    busy = store.create(title="busy")
    store.save_upload(busy.id, "a.md", b"# A\n")
    _seed_outputs(store, busy.id)
    service.start_run(busy.id)  # 占满唯一 pipeline 槽位
    assert started.wait(timeout=5)

    other = store.create(title="other")
    _seed_outputs(store, other.id)

    class ChatLLM:
        def complete(self, system, user):
            return json.dumps({"reply": "ok", "actions": []})

    # pipeline 池被占满时，chat 走独立池仍能被调度
    service._llm_factory = lambda: ChatLLM()
    public = service.start_chat(other.id, "有哪些用例？")
    assert public["status"] == "revising"
    release.set()
    deadline = time.time() + 10
    while time.time() < deadline:
        if service.get_job(other.id)["status"] == "ready":
            break
        time.sleep(0.05)
    assert service.get_job(other.id)["status"] == "ready"


def _seed_outputs(store: JobStore, job_id: str) -> None:
    out = store.output_dir(job_id)
    for name in ("test-plan.md", "risk.md", "coverage-matrix.md"):
        src = FIXTURES / name
        (out / name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    (out / "test-requirements.md").write_text(
        (FIXTURES / "test-requirements-generated.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (out / "testcases.md").write_text(
        (FIXTURES / "testcases-valid.md").read_text(encoding="utf-8"), encoding="utf-8",
    )


# ---- 重启 stale 标记 ----

def test_stale_marking_on_startup(tmp_path):
    root = tmp_path / "jobs"
    store = JobStore(root)
    running = store.create(title="中断")
    store.update(running.id, lambda m: setattr(m, "status", "running"))
    ready = store.create(title="完成")
    store.update(ready.id, lambda m: setattr(m, "status", "ready"))

    restarted = JobStore(root)  # 模拟新进程
    assert restarted.mark_stale_on_startup() == 1
    meta = restarted.load(running.id)
    assert meta.status == "failed"
    assert any("服务重启" in e for e in meta.error or [])
    assert restarted.load(ready.id).status == "ready"


# ---- SSE ----

def test_sse_stream_pushes_until_terminal(tmp_path, mock_responses):
    from qagent.agent.llm import MockLLM
    from qagent.server.app import create_handler

    store = JobStore(tmp_path / "jobs")
    service = QAgentService(store, llm_factory=lambda: MockLLM(mock_responses), max_pipeline=1)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(service))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    try:
        job = service.create_job("t", [("req.md", b"# hello\n")])
        service.start_run(job["id"], "requirements")
        with urlopen(f"http://127.0.0.1:{port}/api/jobs/{job['id']}/events", timeout=30) as resp:
            assert "text/event-stream" in resp.headers.get("Content-Type", "")
            body = resp.read().decode("utf-8")  # 读到流关闭（终态）
        events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
        assert events, "SSE 未推送任何事件"
        assert events[-1]["status"] == "ready"
        assert any(e["status"] == "running" for e in events)
        assert any(e.get("current_step") for e in events)
    finally:
        httpd.shutdown()


# ---- 对话修订 read 回流 ----

def test_chat_read_artifact_reflows_into_second_round(tmp_path):
    store = JobStore(tmp_path / "jobs")
    job = store.create()
    _seed_outputs(store, job.id)

    class TwoRoundLLM:
        def __init__(self):
            self.calls: list[str] = []

        def complete(self, system, user):
            self.calls.append(user)
            if len(self.calls) == 1:
                return json.dumps({
                    "reply": "我先看看",
                    "actions": [{"op": "read_artifact", "name": "cases", "query": "性能"}],
                })
            return json.dumps({
                "reply": "已查看并补充",
                "actions": [{
                    "op": "upsert_cases",
                    "cases": [{
                        "id": "TC-REG-077",
                        "title": "SC-001 回流后补的用例",
                        "priority": "P1",
                        "type": "功能",
                        "preconditions": [],
                        "steps": ["打开注册页"],
                        "expected": "页面可见",
                        "design_method": "场景法",
                        "requirement_ref": "R1",
                    }],
                }],
            })

    from qagent.server.chat import run_chat

    llm = TwoRoundLLM()
    result = run_chat(store, job.id, "看看有没有性能用例", llm)
    assert result["ok"]
    assert len(llm.calls) == 2
    assert "read_artifact 的结果" in llm.calls[1]
    cases = (store.output_dir(job.id) / "testcases.md").read_text(encoding="utf-8")
    assert "TC-REG-077" in cases
