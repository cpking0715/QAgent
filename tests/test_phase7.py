"""文件预览与 AI 审阅：输入文档可预览，产物/输入可发起带交叉参考的审阅。"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from fixtures_loader import mock_responses  # noqa: F401  (pytest fixture)
from qagent.agent.llm import MockLLM
from qagent.server.jobs import JobStore
from qagent.server.service import QAgentService

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _seed_output(store: JobStore, job_id: str) -> None:
    out = store.output_dir(job_id)
    for name in ("test-requirements-generated.md", "test-plan.md", "risk.md",
                 "coverage-matrix.md", "testcases-valid.md"):
        dest = {"test-requirements-generated.md": "test-requirements.md",
                "testcases-valid.md": "testcases.md"}.get(name, name)
        (out / dest).write_text(_fixture_text(name), encoding="utf-8")
    store.refresh_artifacts(job_id)


def _wait_status(service: QAgentService, job_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    got = None
    while time.time() < deadline:
        got = service.get_job(job_id)
        if got["status"] not in {"running", "revising"}:
            return got
        time.sleep(0.05)
    raise AssertionError(f"等待超时，status={got and got['status']}")


def test_input_file_text_reads_markdown(tmp_path):
    store = JobStore(tmp_path / "jobs")
    service = QAgentService(store, llm_factory=lambda: MockLLM({}))
    job = service.create_job("t", [("prd.md", "# 登录\n用户可登录。\n".encode("utf-8"))])
    text = service.input_file_text(job["id"], "prd.md")
    assert "用户可登录" in text
    with pytest.raises(FileNotFoundError):
        service.input_file_text(job["id"], "不存在.md")


def test_input_file_text_rejects_unsupported(tmp_path):
    store = JobStore(tmp_path / "jobs")
    service = QAgentService(store, llm_factory=lambda: MockLLM({}))
    job = store.create()
    store.save_upload(job.id, "logo.png", b"\x89PNG")
    with pytest.raises(ValueError, match="不支持"):
        service.input_file_text(job.id, "logo.png")


def test_review_artifact_with_cross_context(tmp_path):
    """审阅测试用例 → prompt 带用例正文与矩阵参考，结果落对话。"""
    store = JobStore(tmp_path / "jobs")

    class ReviewLLM:
        def __init__(self):
            self.received = ""

        def complete(self, system, user):
            self.received = user
            return "## 总评\n覆盖基本完整。\n## 主要问题\n1. …"

    llm = ReviewLLM()
    service = QAgentService(store, llm_factory=lambda: llm, max_pipeline=2)
    job = store.create()
    store.save_upload(job.id, "prd.md", b"# x\n")
    _seed_output(store, job.id)

    public = service.start_review(job.id, "artifact", "testcases.md")
    assert public["status"] == "revising"
    got = _wait_status(service, job.id)
    assert got["status"] == "ready", got.get("error")
    # prompt 含待审正文与交叉参考矩阵
    assert "TC-REG-001" in llm.received
    assert "SC-001" in llm.received
    assert "参考产物" in llm.received and "覆盖矩阵" in llm.received
    # 结果以标记落对话，前端据此在抽屉渲染
    last = got["chat"][-1]
    assert last["role"] == "assistant"
    assert last["content"].startswith("【审阅·测试用例】")
    assert "总评" in last["content"]


def test_review_input_file(tmp_path):
    store = JobStore(tmp_path / "jobs")
    llm = MockLLM({"__review__": "## 总评\nPRD 描述清晰。"})

    class CaptureLLM:
        received = ""

        def complete(self, system, user):
            CaptureLLM.received = user
            return "## 总评\n输入文档结构清晰。"

    service = QAgentService(store, llm_factory=lambda: CaptureLLM(), max_pipeline=2)
    job = service.create_job("t", [("需求文档.md", "# 注册需求\n手机号注册。\n".encode("utf-8"))])
    service.start_review(job["id"], "input", "需求文档.md")
    got = _wait_status(service, job["id"])
    # awaiting_scope 任务审阅完回到 uploaded，不丢范围确认状态
    assert got["status"] == "uploaded"
    assert "手机号注册" in CaptureLLM.received
    assert "产物清单" in CaptureLLM.received
    assert got["chat"][-1]["content"].startswith("【审阅·需求文档.md】")


def test_review_rejects_while_running(tmp_path):
    store = JobStore(tmp_path / "jobs")
    started = threading.Event()
    release = threading.Event()

    class SlowLLM:
        def complete(self, system, user):
            started.set()
            release.wait(timeout=3)
            return "ok"

    service = QAgentService(store, llm_factory=lambda: SlowLLM(), max_pipeline=2)
    job = store.create()
    store.save_upload(job.id, "req.md", b"# x\n")
    service.start_run(job.id, "requirements")
    assert started.wait(timeout=2)
    with pytest.raises(RuntimeError, match="正在运行"):
        service.start_review(job.id, "input", "req.md")
    release.set()
    _wait_status(service, job.id)


def test_review_validates_target_and_name(tmp_path):
    store = JobStore(tmp_path / "jobs")
    service = QAgentService(store, llm_factory=lambda: MockLLM({}))
    job = store.create()
    store.save_upload(job.id, "req.md", b"# x\n")
    with pytest.raises(ValueError, match="target"):
        service.start_review(job.id, "bogus", "req.md")
    with pytest.raises(FileNotFoundError):
        service.start_review(job.id, "artifact", "test-plan.md")
    with pytest.raises(ValueError, match="Markdown"):
        service.start_review(job.id, "artifact", "testcases.xlsx")


def test_http_input_preview_and_review(tmp_path):
    """HTTP 层：输入预览端点 + 审阅端点返回可轮询状态。"""
    import http.client
    from http.server import ThreadingHTTPServer

    from qagent.server.app import create_handler

    class ReviewLLM:
        def complete(self, system, user):
            return "## 总评\n无阻断问题。"

    store = JobStore(tmp_path / "jobs")
    service = QAgentService(store, llm_factory=lambda: ReviewLLM(), max_pipeline=2)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(service))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    def call(method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            headers = {"Content-Type": "application/json"} if body is not None else {}
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    try:
        job = service.create_job("t", [("prd.md", "# 登录需求\n".encode("utf-8"))])
        job_id = job["id"]
        status, data = call("GET", f"/api/jobs/{job_id}/inputs/prd.md")
        assert status == 200 and "登录需求" in data.decode("utf-8")
        status, _ = call("GET", f"/api/jobs/{job_id}/inputs/missing.md")
        assert status == 404
        # 先种一个产物再审阅
        (store.output_dir(job_id) / "test-plan.md").write_text(
            _fixture_text("test-plan.md"), encoding="utf-8",
        )
        status, data = call(
            "POST", f"/api/jobs/{job_id}/review",
            json.dumps({"target": "artifact", "name": "test-plan.md"}).encode("utf-8"),
        )
        assert status == 200, data
        assert json.loads(data)["status"] == "revising"
        deadline = time.time() + 10
        chat = []
        while time.time() < deadline:
            _, raw = call("GET", f"/api/jobs/{job_id}")
            chat = json.loads(raw)["chat"]
            if chat and chat[-1]["role"] == "assistant" and "【审阅·" in chat[-1]["content"]:
                break
            time.sleep(0.1)
        assert any("【审阅·测试方案】" in m["content"] for m in chat)
    finally:
        httpd.shutdown()
