"""实时产物更新：每步产物落盘后立即可见（不必等整段跑完）。"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from fixtures_loader import mock_responses  # noqa: F401  (pytest fixture)
from qagent.agent.llm import MockLLM
from qagent.server.jobs import JobStore
from qagent.server.service import QAgentService


def test_artifacts_visible_during_run(tmp_path, mock_responses):
    """用例生成被阻塞时，需求/方案/风险/矩阵应已出现在任务产物里（running 状态）。"""
    store = JobStore(tmp_path / "jobs")
    started = threading.Event()
    release = threading.Event()

    class BlockAtCases(MockLLM):
        def complete(self, system, user):
            # 用例 prompt 独有 TC- 示例 + 矩阵行；矩阵 prompt 只有 SC- 示例
            if "TC-" in user and "SC-001" in user and not release.is_set():
                started.set()
                release.wait(timeout=10)
            return super().complete(system, user)

    llm = BlockAtCases(mock_responses)
    service = QAgentService(store, llm_factory=lambda: llm, max_pipeline=2)
    job = store.create()
    store.save_upload(job.id, "req.md", "# 登录\n用户可注册。\n".encode("utf-8"))

    try:
        service.start_run(job.id, "requirements")
        assert started.wait(timeout=30), "未进入用例生成阶段"
        # 阻塞期间（仍在 running）：四份上游产物应已实时可见
        got = service.get_job(job.id)
        assert got["status"] == "running"
        arts = got["artifacts"] or {}
        for key in ("test_requirements", "test_plan", "risk", "coverage_matrix"):
            assert key in arts, f"运行中未见产物 {key}: {sorted(arts)}"
        # 交付物卡片数据同样就绪
        titles = [d["title"] for d in got["deliverables"]]
        assert "测试需求" in titles and "覆盖矩阵" in titles
    finally:
        release.set()

    deadline = time.time() + 30
    while time.time() < deadline:
        got = service.get_job(job.id)
        if got["status"] not in {"running", "revising"}:
            break
        time.sleep(0.1)
    assert got["status"] == "ready", got.get("error")
    assert "testcases" in (got["artifacts"] or {})
