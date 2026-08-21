"""中途开始：上传已写好的产物直接复用，auto 起点缺什么补什么。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from fixtures_loader import mock_responses  # noqa: F401  (pytest fixture)
from qagent.agent.llm import MockLLM
from qagent.server.jobs import JobStore
from qagent.server.service import QAgentService

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _wait_ready(service: QAgentService, job_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    got = None
    while time.time() < deadline:
        got = service.get_job(job_id)
        if got["status"] not in {"running", "revising"}:
            return got
        time.sleep(0.05)
    raise AssertionError(f"等待超时，status={got and got['status']}")


def test_create_job_seeds_recognized_artifacts(tmp_path):
    """上传测试需求/方案/覆盖矩阵 → 原样落为产物，跳过范围澄清。"""
    store = JobStore(tmp_path / "jobs")
    service = QAgentService(store, llm_factory=lambda: MockLLM({}))
    job = service.create_job("t", [
        ("测试需求.md", _fixture("test-requirements-generated.md")),
        ("测试方案.md", _fixture("test-plan.md")),
        ("覆盖矩阵.md", _fixture("coverage-matrix.md")),
    ])
    job_id = job["id"]
    assert job["awaiting_scope"] is False
    out = store.output_dir(job_id)
    for name in ("test-requirements.md", "test-plan.md", "coverage-matrix.md"):
        assert (out / name).is_file(), name
    arts = job["artifacts"] or {}
    assert {"test_requirements", "test_plan", "coverage_matrix"} <= set(arts)
    # 已写好需求 → 导图立即可下载
    assert (out / "test-requirements.drawio").is_file()
    # 阶段推导：矩阵已就绪 → 下一步是用例段
    assert job["stage"]["done"] == "coverage_matrix"
    assert job["stage"]["from"] == "testcases"
    # 对话里说明了识别结果
    assert any("检测到已写好" in m["content"] for m in job["chat"])


def test_auto_run_fills_missing_only(tmp_path, mock_responses):
    """需求/方案/矩阵已写好 → auto 只生成用例及之后，不覆盖已有产物。"""
    store = JobStore(tmp_path / "jobs")
    llm = MockLLM(mock_responses)
    service = QAgentService(store, llm_factory=lambda: llm, max_pipeline=2)
    job = service.create_job("t", [
        ("test-requirements.md", _fixture("test-requirements-generated.md")),
        ("test-plan.md", _fixture("test-plan.md")),
        ("coverage-matrix.md", _fixture("coverage-matrix.md")),
    ])
    job_id = job["id"]
    plan_before = (store.output_dir(job_id) / "test-plan.md").read_bytes()
    matrix_before = (store.output_dir(job_id) / "coverage-matrix.md").read_bytes()

    public = service.start_run(job_id, "auto")
    assert public["status"] == "running"
    got = _wait_ready(service, job_id)
    assert got["status"] == "ready", got.get("error")
    out = store.output_dir(job_id)
    assert (out / "testcases.md").is_file()
    assert (out / "testcases.xlsx").is_file()
    assert got["case_count"] > 0
    # 已写好的产物原样保留
    assert (out / "test-plan.md").read_bytes() == plan_before
    assert (out / "coverage-matrix.md").read_bytes() == matrix_before
    # 方案/矩阵没有走 LLM 重新生成（无对应 prompt 调用）
    joined = " ".join(user for _, user in llm.calls)
    assert "覆盖矩阵" not in joined


def test_auto_run_generates_missing_requirements(tmp_path, mock_responses):
    """只上传方案+矩阵（无需求/无PRD）→ auto 先补需求与风险，再出用例。"""
    store = JobStore(tmp_path / "jobs")
    service = QAgentService(store, llm_factory=lambda: MockLLM(mock_responses), max_pipeline=2)
    job = service.create_job("t", [
        ("test-plan.md", _fixture("test-plan.md")),
        ("coverage-matrix.md", _fixture("coverage-matrix.md")),
    ])
    job_id = job["id"]
    out = store.output_dir(job_id)
    plan_before = (out / "test-plan.md").read_bytes()

    service.start_run(job_id, "auto")
    got = _wait_ready(service, job_id)
    assert got["status"] == "ready", got.get("error")
    assert (out / "test-requirements.md").is_file()
    assert (out / "risk.md").is_file()
    assert (out / "testcases.xlsx").is_file()
    # 用户写好的方案未被覆盖
    assert (out / "test-plan.md").read_bytes() == plan_before


def test_auto_run_reuses_existing_testcases_without_llm(tmp_path):
    """四份文档+用例全部已写好 → auto 复用用例，评审/校验/导出全程零 LLM。"""
    from qagent.parsing import parse_cases

    store = JobStore(tmp_path / "jobs")

    class BoomLLM:
        def complete(self, system, user):
            raise AssertionError("已有全套产物时不应调用 LLM")

    service = QAgentService(store, llm_factory=lambda: BoomLLM(), max_pipeline=2)
    job = service.create_job("t", [
        ("test-requirements.md", _fixture("test-requirements-generated.md")),
        ("test-plan.md", _fixture("test-plan.md")),
        ("risk.md", _fixture("risk.md")),
        ("coverage-matrix.md", _fixture("coverage-matrix.md")),
        ("testcases.md", _fixture("testcases-valid.md")),
    ])
    job_id = job["id"]
    out = store.output_dir(job_id)
    before_ids = {c["id"] for c in parse_cases(out / "testcases.md")}

    service.start_run(job_id, "auto")
    got = _wait_ready(service, job_id)
    assert got["status"] == "ready", got.get("error")
    assert got["case_count"] >= len(before_ids)
    assert (out / "testcases.xlsx").is_file()
    assert (out / "qa-review.md").is_file()
    # 已有用例只增不删（校验补齐缺失场景，不删改已有条目）
    after_ids = {c["id"] for c in parse_cases(out / "testcases.md")}
    assert before_ids <= after_ids


def test_start_run_rejects_unknown_from(tmp_path):
    store = JobStore(tmp_path / "jobs")
    service = QAgentService(store, llm_factory=lambda: MockLLM({}))
    job = store.create()
    store.save_upload(job.id, "req.md", b"# hello\n")
    with pytest.raises(ValueError, match="无效起点"):
        service.start_run(job.id, "bogus_step")


def test_start_run_from_plan_without_requirements_fails(tmp_path, mock_responses):
    """从方案起跑但缺少需求 → runner 报缺失产物，任务失败且信息明确。"""
    store = JobStore(tmp_path / "jobs")
    service = QAgentService(store, llm_factory=lambda: MockLLM(mock_responses), max_pipeline=2)
    job = store.create()
    store.save_upload(job.id, "req.md", b"# hello\n")
    service.start_run(job.id, "test_plan")
    got = _wait_ready(service, job_id=job.id)
    assert got["status"] == "failed"
    assert any("续跑缺少产物" in e for e in (got.get("error") or []))
