"""端到端验证：HTTP 全链路（上传→澄清→生成→SSE→下载→修订→删除）与 CLI --mock。

所有 HTTP 请求只允许访问 127.0.0.1 上本测试自建的临时服务（回环白名单）。
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from qagent.agent.llm import MockLLM
from qagent.server.app import create_handler
from qagent.server.jobs import JobStore
from qagent.server.service import QAgentService
from fixtures_loader import mock_responses

REPO = Path(__file__).resolve().parents[1]

_UPSERT_CHAT = json.dumps({
    "reply": "已补充一条用例",
    "actions": [
        {
            "op": "upsert_cases",
            "cases": [{
                "id": "TC-REG-088",
                "title": "SC-001 端到端补充用例",
                "priority": "P1",
                "type": "功能",
                "preconditions": [],
                "steps": ["打开注册页"],
                "expected": "页面可见",
                "design_method": "场景法",
                "requirement_ref": "R1",
            }],
        },
        {"op": "validate_and_export", "fill_gaps": True},
    ],
}, ensure_ascii=False)


class E2ELLM:
    """流水线走 Mock 响应；对话修订返回固定 upsert 动作。"""

    def __init__(self, responses: dict[str, str]):
        self._mock = MockLLM(responses)

    def complete(self, system: str, user: str) -> str:
        if "最近对话" in user and "用户：" in user:
            return _UPSERT_CHAT
        return self._mock.complete(system, user)


class LocalClient:
    """仅连接 127.0.0.1 测试服务的 HTTP 客户端（回环白名单，无外部访问）。"""

    def __init__(self, port: int) -> None:
        if not isinstance(port, int):
            raise ValueError("port 必须为整数")
        self._port = port

    def request(
        self, method: str, path: str,
        body: bytes | None = None, headers: dict | None = None,
    ) -> tuple[int, str, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self._port, timeout=60)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()
        finally:
            conn.close()

    def get_json(self, path: str):
        status, _, data = self.request("GET", path)
        assert status == 200, f"GET {path} -> {status}"
        return json.loads(data.decode("utf-8"))


def test_http_end_to_end(tmp_path, mock_responses):
    store = JobStore(tmp_path / "jobs")
    service = QAgentService(
        store, llm_factory=lambda: E2ELLM(mock_responses), max_pipeline=2,
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(service))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    client = LocalClient(httpd.server_address[1])
    try:
        # 1. 首页
        status, ctype, page = client.request("GET", "/")
        assert status == 200 and b"<html" in page.lower()

        # 2. multipart 上传（手写解析器）→ 触发范围澄清
        boundary = "----E2EBoundary"
        payload = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="files"; filename="prd.md"\r\n'
            "Content-Type: text/markdown\r\n\r\n"
            "# 登录功能\n用户可用手机号注册并登录。\n"
            f"\r\n--{boundary}--\r\n"
        ).encode("utf-8")
        status, _, data = client.request(
            "POST", "/api/jobs", body=payload,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        assert status == 200, data
        job = json.loads(data.decode("utf-8"))
        job_id = job["id"]
        assert job["awaiting_scope"] is True
        assert any("测试范围" in m.get("content", "") for m in job["chat"])

        # 3. 回复测试范围 → 写入测试需求并自动起跑（分段模式：先只生成测试需求）
        status, _, data = client.request(
            "POST", f"/api/jobs/{job_id}/chat",
            body=json.dumps({"message": "不测性能，主流程和接口"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        assert status == 200, data
        public = json.loads(data.decode("utf-8"))
        assert public["status"] in {"revising", "running"}

        def sse_until_ready(job_id: str) -> dict:
            status, ctype, body = client.request("GET", f"/api/jobs/{job_id}/events")
            assert status == 200 and "text/event-stream" in ctype
            events = [
                json.loads(line[6:])
                for line in body.decode("utf-8").splitlines()
                if line.startswith("data: ")
            ]
            assert events, "SSE 未推送事件"
            assert events[-1]["status"] == "ready", [e["status"] for e in events]
            return events[-1]

        # 段 1：测试需求（生成后停下等待确认）
        final = sse_until_ready(job_id)
        assert "test_requirements" in (final["artifacts"] or {})
        assert not (store.output_dir(job_id) / "test-plan.md").exists()
        assert final["stage"]["done"] == "test_requirements"
        assert "阶段完成" in (final["current_step"] or "")

        # 人工在线编辑测试需求（PUT 产物）
        edited = (
            (store.output_dir(job_id) / "test-requirements.md").read_text(encoding="utf-8")
            + "\n## 11. 人工补充要点\n- 必测离线登录\n"
        )
        status, _, data = client.request(
            "PUT", f"/api/jobs/{job_id}/artifacts/test-requirements.md",
            body=json.dumps({"content": edited}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        assert status == 200, data
        assert "人工补充要点" in (
            store.output_dir(job_id) / "test-requirements.md"
        ).read_text(encoding="utf-8")

        # 段 2：方案 + 风险 + 矩阵（以修改后的需求为输入）
        status, _, data = client.request(
            "POST", f"/api/jobs/{job_id}/run",
            body=json.dumps({"from": "test_plan", "stop_after": "coverage_matrix"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        assert status == 200, data
        final = sse_until_ready(job_id)
        assert final["stage"]["done"] == "coverage_matrix"
        assert "coverage_matrix" in (final["artifacts"] or {})
        assert not (store.output_dir(job_id) / "testcases.md").exists()

        # 段 3：用例 + Review + 导出
        status, _, data = client.request(
            "POST", f"/api/jobs/{job_id}/run",
            body=json.dumps({"from": "testcases"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        assert status == 200, data
        final = sse_until_ready(job_id)
        assert final["case_count"] and final["case_count"] > 0
        artifacts = final["artifacts"] or {}
        for key in ("test_requirements", "coverage_matrix", "testcases", "qa_review", "xlsx"):
            assert key in artifacts, f"缺少产物 {key}"
        assert (store.input_dir(job_id) / "测试需求.md").is_file()

        # 5. 下载二进制产物（xlsx = zip 头）
        status, _, data = client.request("GET", f"/api/jobs/{job_id}/artifacts/testcases.xlsx")
        assert status == 200 and data[:2] == b"PK"

        # 6. 对话修订（upsert + 校验导出）
        status, _, _ = client.request(
            "POST", f"/api/jobs/{job_id}/chat",
            body=json.dumps({"message": "给 R1 再补一条注册用例"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        assert status == 200
        deadline = time.time() + 30
        got = None
        while time.time() < deadline:
            got = client.get_json(f"/api/jobs/{job_id}")
            if got["status"] not in {"revising", "running"}:
                break
            time.sleep(0.2)
        assert got is not None and got["status"] == "ready", got and got["status"]
        cases_text = (store.output_dir(job_id) / "testcases.md").read_text(encoding="utf-8")
        assert "TC-REG-088" in cases_text
        review_text = (store.output_dir(job_id) / "qa-review.md").read_text(encoding="utf-8")
        assert "SC-001" in review_text and "追溯表" in review_text

        # 7. 删除 → 404
        status, _, data = client.request("DELETE", f"/api/jobs/{job_id}")
        assert status == 200 and json.loads(data.decode("utf-8"))["ok"] is True
        status, _, _ = client.request("GET", f"/api/jobs/{job_id}")
        assert status == 404
    finally:
        httpd.shutdown()


def test_cli_run_mock_end_to_end(tmp_path, monkeypatch, capsys):
    """进程内走 CLI 完整链路：ingest → 生成 → 校验 → 导出（与 `qagent run --mock` 同一路径）。"""
    from qagent import cli

    out = tmp_path / "out"
    monkeypatch.chdir(tmp_path)  # workspace 指向临时目录，不污染仓库
    code = cli.main([
        "run", str(REPO / "input" / "requirement-example.md"),
        "--mock", "--out", str(out),
    ])
    captured = capsys.readouterr()
    assert code == 0, f"stdout={captured.out}\nstderr={captured.err}"
    for name in (
        "test-requirements.md", "test-plan.md", "risk.md", "coverage-matrix.md",
        "testcases.md", "qa-review.md", "testcases.xlsx", "test-requirements.drawio",
    ):
        assert (out / name).is_file(), f"CLI 端到端缺少产物 {name}"
    assert "条用例" in captured.out
