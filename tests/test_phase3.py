"""阶段 3 回归：共享校验、结构化进度回调、步级断点续跑。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from qagent.agent.llm import MockLLM
from qagent.agent.runner import QAgentRunner
from qagent.config import LLMConfig, QAgentConfig
from qagent.validation import full_validate

FIXTURES = Path(__file__).parent / "fixtures"
REPO = Path(__file__).resolve().parents[1]


def _config(tmp_path: Path) -> QAgentConfig:
    out = tmp_path / "output"
    return QAgentConfig(
        workspace=REPO,
        input_dir=REPO / "input",
        output_dir=out,
        schema_path=REPO / "templates" / "testcase.schema.yaml",
        templates_dir=REPO / "templates",
        retry_limit=2,
        llm=LLMConfig(),
    )


def _seed_upstream(out: Path, names=("test-requirements", "test-plan", "risk", "coverage-matrix")) -> None:
    mapping = {
        "test-requirements": "test-requirements-generated.md",
        "test-plan": "test-plan.md",
        "risk": "risk.md",
        "coverage-matrix": "coverage-matrix.md",
    }
    out.mkdir(parents=True, exist_ok=True)
    for key in names:
        src = FIXTURES / mapping[key]
        (out / f"{key}.md").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")


def test_full_validate_over_seeded_artifacts(tmp_path):
    config = _config(tmp_path)
    _seed_upstream(config.output_dir)
    (config.output_dir / "testcases.md").write_text(
        (FIXTURES / "testcases-valid.md").read_text(encoding="utf-8"), encoding="utf-8",
    )
    from qagent.parsing import render_qa_review_md, parse_coverage_matrix

    rows = parse_coverage_matrix(config.coverage_matrix_path)
    from qagent.parsing import parse_cases

    cases = parse_cases(config.testcases_path)
    (config.output_dir / "qa-review.md").write_text(
        render_qa_review_md(rows, cases), encoding="utf-8",
    )
    outcome = full_validate(config)
    assert outcome.errors == [], outcome.errors
    assert len(outcome.cases) == 3
    assert outcome.requirement_ids


def test_on_step_reports_structured_progress(tmp_path, mock_style_responses):
    config = _config(tmp_path)
    seen: list[tuple[str, int, int]] = []

    def on_step(step_id: str, index: int, total: int, label: str) -> None:
        seen.append((step_id, index, total))

    runner = QAgentRunner(config, MockLLM(mock_style_responses), on_step=on_step)
    result = runner.run(REPO / "input" / "requirement-example.md")
    assert result.success
    ids = [item[0] for item in seen]
    assert ids == [
        "test_requirements", "test_plan", "risk", "coverage_matrix",
        "testcases", "qa_review", "validate", "export",
    ]
    assert [item[1] for item in seen] == [2, 3, 4, 5, 6, 7, 8, 9]
    assert all(item[2] == 9 for item in seen)


def test_auto_resume_reuses_all_upstream(tmp_path):
    config = _config(tmp_path)
    _seed_upstream(config.output_dir)
    responses = {
        "testcases": (FIXTURES / "testcases-valid.md").read_text(encoding="utf-8"),
        "__fix__": (FIXTURES / "testcases-valid.md").read_text(encoding="utf-8"),
    }
    llm = MockLLM(responses)
    runner = QAgentRunner(config, llm)
    result = runner.run(REPO / "input" / "requirement-example.md", start_from="auto")

    assert result.success
    assert "test_requirements" in result.steps_completed  # 复用已补记
    assert "coverage_matrix" in result.steps_completed
    # 上游生成步骤未被调用（auto 只补缺失及之后）
    joined = "\n".join(user for _, user in llm.calls)
    assert "生成完整的 test-requirements.md" not in joined
    assert "生成完整的 test-plan.md" not in joined
    assert "生成完整的 risk.md" not in joined
    assert "生成完整的 coverage-matrix.md" not in joined


def test_auto_resume_from_first_missing_step(tmp_path):
    config = _config(tmp_path)
    _seed_upstream(config.output_dir, names=("test-requirements", "test-plan"))  # 缺 risk/矩阵
    llm = MockLLM({
        "risk.md": (FIXTURES / "risk.md").read_text(encoding="utf-8"),
        "coverage-matrix": (FIXTURES / "coverage-matrix.md").read_text(encoding="utf-8"),
        "__fix_matrix__": (FIXTURES / "coverage-matrix.md").read_text(encoding="utf-8"),
        "testcases": (FIXTURES / "testcases-valid.md").read_text(encoding="utf-8"),
        "__fix__": (FIXTURES / "testcases-valid.md").read_text(encoding="utf-8"),
    })
    runner = QAgentRunner(config, llm)
    result = runner.run(REPO / "input" / "requirement-example.md", start_from="auto")

    assert result.success
    joined = "\n".join(user for _, user in llm.calls)
    assert "生成完整的 test-requirements.md" not in joined
    assert "生成完整的 test-plan.md" not in joined
    assert "生成完整的 risk.md" in joined  # 从 risk 步骤起跑


def test_resume_missing_artifact_reports_error(tmp_path):
    config = _config(tmp_path)  # 未 seed 任何产物
    runner = QAgentRunner(config, MockLLM({}))
    result = runner.run(REPO / "input" / "requirement-example.md", start_from="testcases")
    assert not result.success
    assert any("续跑缺少产物" in err for err in result.errors)


@pytest.fixture
def mock_style_responses():
    return {
        "test-requirements": (FIXTURES / "test-requirements-generated.md").read_text(encoding="utf-8"),
        "test-plan": (FIXTURES / "test-plan.md").read_text(encoding="utf-8"),
        "risk.md": (FIXTURES / "risk.md").read_text(encoding="utf-8"),
        "testcases": (FIXTURES / "testcases-valid.md").read_text(encoding="utf-8"),
        "__fix__": (FIXTURES / "testcases-valid.md").read_text(encoding="utf-8"),
        "coverage-matrix": (FIXTURES / "coverage-matrix.md").read_text(encoding="utf-8"),
        "qa-review": (FIXTURES / "qa-review.md").read_text(encoding="utf-8"),
        "__fix_matrix__": (FIXTURES / "coverage-matrix.md").read_text(encoding="utf-8"),
    }


def test_service_current_step_set_via_callback(tmp_path, mock_style_responses):
    from qagent.server.jobs import JobStore
    from qagent.server.service import QAgentService

    store = JobStore(tmp_path / "jobs")
    service = QAgentService(store, llm_factory=lambda: MockLLM(mock_style_responses), max_pipeline=1)
    job = store.create()
    store.save_upload(job.id, "req.md", (REPO / "input" / "requirement-example.md").read_bytes())
    service.start_run(job.id, "requirements")
    final = None
    for _ in range(200):
        got = service.get_job(job.id)
        if got["status"] in {"ready", "failed", "cancelled"}:
            final = got
            break
        time.sleep(0.05)
    assert final is not None and final["status"] == "ready", final
    assert final["current_step"] == "9/9 完成"
