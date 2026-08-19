"""任务存储、修订工具与 HTTP/飞书适配。"""

from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from qagent.agent.llm import MockLLM
from qagent.server.app import create_handler
from qagent.server.chat import apply_actions, run_chat
from qagent.server.feishu import handle_feishu_event
from qagent.server.jobs import JobStore
from qagent.server.service import QAgentService
from qagent.server.tools import delete_cases, patch_plan, upsert_cases, validate_and_export

FIXTURES = Path(__file__).parent / "fixtures"


def _seed_output(store: JobStore, job_id: str) -> None:
    out = store.output_dir(job_id)
    for name in ("test-plan.md", "risk.md", "coverage-matrix.md", "testcases-valid.md"):
        src = FIXTURES / name
        dest = out / ("testcases.md" if name == "testcases-valid.md" else name)
        dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    review = FIXTURES / "qa-review.md"
    if review.is_file():
        (out / "qa-review.md").write_text(review.read_text(encoding="utf-8"), encoding="utf-8")


def test_job_store_isolation(tmp_path):
    store = JobStore(tmp_path / "jobs")
    a = store.create(owner="alice", title="A")
    b = store.create(owner="bob", title="B")
    store.save_upload(a.id, "prd.md", b"# A\n")
    store.save_upload(b.id, "prd.md", b"# B\n")
    assert (store.input_dir(a.id) / "prd.md").read_text() == "# A\n"
    assert (store.input_dir(b.id) / "prd.md").read_text() == "# B\n"
    assert len(store.list_jobs()) == 2
    assert store.load(a.id).owner == "alice"


def test_delete_job_removes_files(tmp_path):
    store = JobStore(tmp_path / "jobs")
    job = store.create(title="gone")
    store.save_upload(job.id, "req.md", b"# x\n")
    store.delete(job.id)
    with pytest.raises(FileNotFoundError):
        store.load(job.id)
    assert not (tmp_path / "jobs" / job.id).exists()


def test_delete_running_job_rejected(tmp_path):
    store = JobStore(tmp_path / "jobs")
    job = store.create()
    meta = store.load(job.id)
    meta.status = "running"
    store.save_meta(meta)
    with pytest.raises(RuntimeError, match="执行中"):
        store.delete(job.id)
    assert store.load(job.id).id == job.id


def test_start_run_clears_previous_error_and_logs(tmp_path):
    store = JobStore(tmp_path / "jobs")
    service = QAgentService(store, llm_factory=lambda: MockLLM({}))
    job = store.create()
    store.save_upload(job.id, "req.md", b"# hello\n")
    meta = store.load(job.id)
    meta.status = "failed"
    meta.error = ["读取 PDF 需要安装 pypdf"]
    meta.logs = ["[19:21:31] ERROR: 读取 PDF 需要安装 pypdf (OCR-PRD.pdf)"]
    store.save_meta(meta)
    public = service.start_run(job.id, "requirements")
    assert public["status"] == "running"
    assert public["error"] is None
    assert public["logs"] == []


def test_start_run_allows_parallel_jobs(tmp_path):
    store = JobStore(tmp_path / "jobs")
    service = QAgentService(store, llm_factory=lambda: MockLLM({}), max_pipeline=2)
    a = store.create(title="A")
    b = store.create(title="B")
    store.save_upload(a.id, "a.md", b"# A\n")
    store.save_upload(b.id, "b.md", b"# B\n")
    ra = service.start_run(a.id)
    rb = service.start_run(b.id)
    assert ra["status"] == "running"
    assert rb["status"] == "running"
    assert ra["id"] != rb["id"]


def test_http_delete_job(tmp_path):
    store = JobStore(tmp_path / "jobs")
    service = QAgentService(store, llm_factory=lambda: MockLLM({}))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(service))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    try:
        job = service.create_job("t", [("req.md", b"# hello\n")])
        req = Request(f"http://127.0.0.1:{port}/api/jobs/{job['id']}", method="DELETE")
        body = json.loads(urlopen(req, timeout=3).read())
        assert body["ok"] is True
        with pytest.raises(HTTPError) as exc:
            urlopen(f"http://127.0.0.1:{port}/api/jobs/{job['id']}", timeout=3)
        assert exc.value.code == 404
        listed = json.loads(urlopen(f"http://127.0.0.1:{port}/api/jobs", timeout=3).read())
        assert listed["jobs"] == []
    finally:
        httpd.shutdown()


def test_patch_upsert_delete_and_validate(tmp_path):
    store = JobStore(tmp_path / "jobs")
    job = store.create()
    _seed_output(store, job.id)
    patch_plan(store, job.id, add=[{"id": "R9", "text": "补一条可验证规则"}])
    text = (store.output_dir(job.id) / "test-plan.md").read_text(encoding="utf-8")
    assert "R9: 补一条可验证规则" in text
    patch_plan(store, job.id, edit={"R9": ""})  # 不把额外 R 带进校验
    (store.output_dir(job.id) / "test-plan.md").write_text(
        (FIXTURES / "test-plan.md").read_text(encoding="utf-8"), encoding="utf-8",
    )
    upsert_cases(store, job.id, [{
        "id": "TC-REG-009",
        "title": "SC-001 额外说明",
        "priority": "P1",
        "type": "功能",
        "preconditions": [],
        "steps": ["打开注册页"],
        "expected": "页面可见",
        "design_method": "场景法",
        "requirement_ref": "R1",
    }])
    delete_cases(store, job.id, ["TC-REG-009"])
    result = validate_and_export(store, job.id, fill_gaps=True)
    assert result["ok"], result.get("errors")
    assert (store.output_dir(job.id) / "testcases.xlsx").is_file()
    assert (store.output_dir(job.id) / "test-plan-mindmap.md").is_file()


def test_apply_actions_rollback_on_bad_delete(tmp_path):
    store = JobStore(tmp_path / "jobs")
    job = store.create()
    _seed_output(store, job.id)
    before = (store.output_dir(job.id) / "testcases.md").read_text(encoding="utf-8")
    from qagent.server.tools import restore_snapshot, snapshot_output
    snapshot_output(store, job.id)
    try:
        apply_actions(store, job.id, [
            {"op": "delete_cases", "ids": ["TC-REG-001", "TC-REG-002", "TC-REG-003"]},
            {"op": "validate_and_export", "fill_gaps": False},
        ])
    except ValueError:
        restore_snapshot(store, job.id)
    after = (store.output_dir(job.id) / "testcases.md").read_text(encoding="utf-8")
    assert after == before


def test_run_chat_json_actions(tmp_path):
    store = JobStore(tmp_path / "jobs")
    job = store.create()
    _seed_output(store, job.id)
    class JsonLLM:
        def complete(self, system, user):
            return json.dumps({
                "reply": "已补用例",
                "actions": [
                    {
                        "op": "upsert_cases",
                        "cases": [{
                            "id": "TC-REG-008",
                            "title": "SC-001 对话补的用例",
                            "priority": "P1",
                            "type": "功能",
                            "preconditions": [],
                            "steps": ["打开注册"],
                            "expected": "可打开",
                            "design_method": "场景法",
                            "requirement_ref": "R1",
                        }],
                    },
                    {"op": "validate_and_export", "fill_gaps": True},
                ],
            })
    result = run_chat(store, job.id, "给 R1 补一条用例", JsonLLM())
    assert result["ok"], result
    cases = (store.output_dir(job.id) / "testcases.md").read_text(encoding="utf-8")
    assert "TC-REG-008" in cases


def test_http_multipart_upload_creates_job(tmp_path):
    store = JobStore(tmp_path / "jobs")
    service = QAgentService(store, llm_factory=lambda: MockLLM({}))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(service))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    try:
        boundary = "----QAgentTestBoundary"
        payload = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="files"; filename="req.md"\r\n'
            "Content-Type: text/markdown\r\n"
            "\r\n"
            "# 登录\n用户可登录。\n"
            f"\r\n--{boundary}--\r\n"
        ).encode("utf-8")
        req = Request(
            f"http://127.0.0.1:{port}/api/jobs",
            data=payload,
            method="POST",
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "X-User": "alice",
            },
        )
        job = json.loads(urlopen(req, timeout=3).read())
        assert job["id"]
        got = json.loads(urlopen(f"http://127.0.0.1:{port}/api/jobs/{job['id']}", timeout=3).read())
        assert "req.md" in got["inputs"]
    finally:
        httpd.shutdown()


def test_http_jobs_and_health(tmp_path):
    store = JobStore(tmp_path / "jobs")
    service = QAgentService(store, llm_factory=lambda: MockLLM({}))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(service))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    try:
        health = json.loads(urlopen(f"http://127.0.0.1:{port}/health", timeout=3).read())
        assert health["ok"]
        listed = json.loads(urlopen(f"http://127.0.0.1:{port}/api/jobs", timeout=3).read())
        assert listed["jobs"] == []
        job = service.create_job("t", [("req.md", b"# hello\n")])
        got = json.loads(urlopen(f"http://127.0.0.1:{port}/api/jobs/{job['id']}", timeout=3).read())
        assert got["id"] == job["id"]
        assert "req.md" in got["inputs"]
    finally:
        httpd.shutdown()


def test_feishu_url_verification(tmp_path):
    service = QAgentService(JobStore(tmp_path / "jobs"), llm_factory=lambda: MockLLM({}))
    out = handle_feishu_event(service, {"type": "url_verification", "challenge": "abc"})
    assert out["challenge"] == "abc"
