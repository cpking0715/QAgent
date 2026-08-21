"""分段工作流（阶段确认模式）：stop_after 停止点、人工修改产物后续跑、阶段推导。"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from qagent.agent.llm import MockLLM
from qagent.agent.runner import QAgentRunner
from qagent.config import LLMConfig, QAgentConfig
from qagent.server.jobs import JobStore
from qagent.server.service import QAgentService
from fixtures_loader import FIXTURES, mock_responses

REPO = Path(__file__).resolve().parents[1]


def _config(tmp_path: Path) -> QAgentConfig:
    return QAgentConfig(
        workspace=REPO,
        input_dir=REPO / "input",
        output_dir=tmp_path / "output",
        schema_path=REPO / "templates" / "testcase.schema.yaml",
        templates_dir=REPO / "templates",
        retry_limit=2,
        llm=LLMConfig(),
    )


def _wait_ready(service: QAgentService, job_id: str, timeout: float = 30) -> dict:
    deadline = time.time() + timeout
    got = None
    while time.time() < deadline:
        got = service.get_job(job_id)
        if got["status"] not in {"running", "revising"}:
            return got
        time.sleep(0.05)
    return got


def test_runner_stops_after_test_requirements(tmp_path, mock_responses):
    config = _config(tmp_path)
    runner = QAgentRunner(config, MockLLM(mock_responses))
    result = runner.run(
        REPO / "input" / "requirement-example.md",
        start_from="requirements",
        stop_after="test_requirements",
    )
    assert result.success
    assert result.stopped_after == "test_requirements"
    assert (config.output_dir / "test-requirements.md").is_file()
    assert not (config.output_dir / "test-plan.md").exists()  # 后续步骤未执行
    assert not (config.output_dir / "testcases.md").exists()


def test_runner_rejects_invalid_stop_point(tmp_path, mock_responses):
    config = _config(tmp_path)
    runner = QAgentRunner(config, MockLLM(mock_responses))
    with pytest.raises(ValueError, match="无效停止点"):
        runner.run(REPO / "input" / "requirement-example.md", stop_after="nope")


def test_phased_workflow_with_manual_edit(tmp_path, mock_responses):
    """三段式 + 人工修改：修改后的测试需求必须进入下一阶段 prompt。"""
    config = _config(tmp_path)
    llm = MockLLM(mock_responses)
    runner = QAgentRunner(config, llm)

    # 段 1：测试需求
    r1 = runner.run(
        REPO / "input" / "requirement-example.md",
        stop_after="test_requirements",
    )
    assert r1.stopped_after == "test_requirements"

    # 人工修改测试需求（模拟在线编辑）
    treq = config.output_dir / "test-requirements.md"
    edited = treq.read_text(encoding="utf-8") + "\n## 11. 人工补充要点\n- 必测离线场景\n"
    treq.write_text(edited, encoding="utf-8")

    # 段 2：方案 + 风险 + 矩阵（复用修改后的需求）
    r2 = runner.run(
        REPO / "input" / "requirement-example.md",
        start_from="test_plan",
        stop_after="coverage_matrix",
    )
    assert r2.stopped_after == "coverage_matrix"
    assert (config.output_dir / "test-plan.md").is_file()
    assert (config.output_dir / "coverage-matrix.md").is_file()
    assert not (config.output_dir / "testcases.md").exists()
    plan_prompt = next(u for _, u in llm.calls if "生成完整的 test-plan.md" in u)
    assert "人工补充要点" in plan_prompt  # 修改内容进入下一阶段输入

    # 段 3：用例（复用方案与矩阵）
    r3 = runner.run(REPO / "input" / "requirement-example.md", start_from="testcases")
    assert r3.success and r3.stopped_after is None
    assert (config.output_dir / "testcases.xlsx").is_file()


def test_service_stage_progression(tmp_path, mock_responses):
    store = JobStore(tmp_path / "jobs")
    service = QAgentService(store, llm_factory=lambda: MockLLM(mock_responses), max_pipeline=2)
    job = service.create_job("t", [("req.md", (REPO / "input" / "requirement-example.md").read_bytes())])
    job_id = job["id"]
    assert job["awaiting_scope"] is True

    # 范围确认 → 自动只跑段 1
    public = service.chat(job_id, "全量")
    assert public["rerun"] == "requirements"
    got = _wait_ready(service, job_id)
    assert got["status"] == "ready", got["status"]
    stage = got["stage"]
    assert stage["done"] == "test_requirements"
    assert stage["from"] == "auto"
    assert not (store.output_dir(job_id) / "test-plan.md").exists()

    # 继续段 2
    service.start_run(job_id, stage["from"], stop_after=stage["stop_after"])
    got = _wait_ready(service, job_id)
    assert got["status"] == "ready"
    assert got["stage"]["done"] == "coverage_matrix"

    # 继续段 3 → 全部完成
    service.start_run(job_id, got["stage"]["from"])
    got = _wait_ready(service, job_id)
    assert got["status"] == "ready"
    assert got["stage"]["done"] == "export"
    assert got["case_count"] > 0
    assert (store.output_dir(job_id) / "testcases.xlsx").is_file()


def test_save_artifact_edits_markdown_only(tmp_path, mock_responses):
    store = JobStore(tmp_path / "jobs")
    service = QAgentService(store, llm_factory=lambda: MockLLM(mock_responses))
    job = store.create()
    out = store.output_dir(job.id)
    (out / "test-requirements.md").write_text("# 原始需求\n", encoding="utf-8")

    updated = service.save_artifact(job.id, "test-requirements.md", "# 改后需求\n\n- 补充要点\n")
    assert "改后需求" in (out / "test-requirements.md").read_text(encoding="utf-8")
    assert (out / "test-requirements.xmind").is_file()  # 导图随正文更新
    assert updated["artifacts"].get("test_requirements")

    with pytest.raises(ValueError, match="Markdown"):
        service.save_artifact(job.id, "testcases.xlsx", "x")
    with pytest.raises(FileNotFoundError):
        service.save_artifact(job.id, "risk.md", "不存在的内容")
