"""本地化打开、审阅终止、测试需求详细化提示。"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from qagent.agent.llm import LLMCancelled, MockLLM
from qagent.agent.prompts import build_test_requirements_prompt
from qagent.config import resolve_config
from qagent.server.jobs import JobStore
from qagent.server.service import QAgentService

FIXTURES = Path(__file__).parent / "fixtures"


def _seed_plan(store: JobStore, job_id: str) -> None:
    (store.output_dir(job_id) / "test-plan.md").write_text(
        (FIXTURES / "test-plan.md").read_text(encoding="utf-8"), encoding="utf-8",
    )
    store.refresh_artifacts(job_id)


def _wait_terminal(service: QAgentService, job_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    got = None
    while time.time() < deadline:
        got = service.get_job(job_id)
        if got["status"] not in {"running", "revising"}:
            return got
        time.sleep(0.05)
    raise AssertionError(f"等待超时，status={got and got['status']}")


def test_open_file_uses_system_editor(tmp_path, monkeypatch):
    """编辑器打开：白名单文件名 + 列表参数调用系统 open。"""
    calls: list[list[str]] = []

    class FakeProc:
        returncode = 0
        stderr = b""

    monkeypatch.setattr(
        "qagent.server.service.subprocess.run", lambda cmd, **kw: calls.append(cmd) or FakeProc(),
    )
    store = JobStore(tmp_path / "jobs")
    service = QAgentService(store, llm_factory=lambda: MockLLM({}))
    job = store.create()
    store.save_upload(job.id, "prd.md", b"# x\n")
    _seed_plan(store, job.id)

    out = service.open_file(job.id, "artifact", "test-plan.md")
    assert out["ok"] is True
    assert calls and calls[-1][0] in {"open", "xdg-open"}
    assert calls[-1][-1].endswith("test-plan.md")
    service.open_file(job.id, "input", "prd.md")
    assert calls[-1][-1].endswith("prd.md")


def test_open_file_rejects_bad_names(tmp_path):
    store = JobStore(tmp_path / "jobs")
    service = QAgentService(store, llm_factory=lambda: MockLLM({}))
    job = store.create()
    with pytest.raises(ValueError, match="非法文件名"):
        service.open_file(job.id, "artifact", "a;rm -rf.md")
    with pytest.raises(ValueError, match="target"):
        service.open_file(job.id, "bogus", "test-plan.md")
    with pytest.raises(FileNotFoundError):
        service.open_file(job.id, "artifact", "不存在.md")


def test_review_cancel_marks_terminated(tmp_path):
    """审阅中终止：状态 cancelled，对话落「已终止」，不再输出审阅结果。"""
    store = JobStore(tmp_path / "jobs")
    started = threading.Event()
    release = threading.Event()

    class SlowReviewLLM:
        def complete(self, system, user):
            started.set()
            release.wait(timeout=5)
            return "## 总评\n迟到的审阅结果不应出现"

    service = QAgentService(store, llm_factory=lambda: SlowReviewLLM(), max_pipeline=2)
    job = store.create()
    _seed_plan(store, job.id)
    public = service.start_review(job.id, "artifact", "test-plan.md")
    assert public["status"] == "revising"
    assert started.wait(timeout=2)
    service.cancel_job(job.id)
    release.set()
    got = _wait_terminal(service, job.id)
    assert got["status"] == "cancelled"
    last = got["chat"][-1]["content"]
    assert last.startswith("【审阅·测试方案】")
    assert "已终止" in last
    assert "迟到的审阅结果" not in last


def test_review_stream_cancel_raises_llm_cancelled(tmp_path):
    """流式取消路径：LLMCancelled → 已终止。"""
    store = JobStore(tmp_path / "jobs")
    started = threading.Event()
    release = threading.Event()

    class CancelledLLM:
        def complete(self, system, user):
            started.set()
            release.wait(timeout=5)
            raise LLMCancelled("stream aborted")

    service = QAgentService(store, llm_factory=lambda: CancelledLLM(), max_pipeline=2)
    job = store.create()
    _seed_plan(store, job.id)
    service.start_review(job.id, "artifact", "test-plan.md")
    assert started.wait(timeout=2)
    service.cancel_job(job.id)
    release.set()
    got = _wait_terminal(service, job.id)
    assert got["status"] == "cancelled"
    assert "已终止" in got["chat"][-1]["content"]


def test_requirements_prompt_scope_is_constraint_not_cap(tmp_path):
    """范围说明只约束测不测，不限制清单详尽度；无输入时强制从文档详尽提取。"""
    config = resolve_config(overrides={"output_dir": str(tmp_path / "out")})
    brief_scope = (
        "# PRD\n用户可注册登录。\n\n## 测试需求\n\n### 范围\n不测性能，主流程和接口\n"
    )
    _sys, user = build_test_requirements_prompt(brief_scope, Path("req.md"), config)
    assert "范围约束" in user and "内容上限" in user
    assert "逐模块" in user and "宁多勿漏" in user
    # 硬性要求包含详尽提取条款
    assert "逐条提取" in user and "禁止" in user and "写薄" in user
    # 无测试需求输入时同样要求详尽
    _sys2, user2 = build_test_requirements_prompt("# PRD\n只有产品内容\n", Path("req.md"), config)
    assert "逐条提取" in user2
