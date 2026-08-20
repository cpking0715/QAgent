"""独立 Agent 集成测试。"""

from pathlib import Path

import pytest

from qagent.agent.llm import MockLLM
from qagent.agent.prompts import extract_document
from qagent.agent.runner import QAgentRunner
from qagent.config import QAgentConfig, LLMConfig

FIXTURES = Path(__file__).parent / "fixtures"
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def mock_responses():
    return {
        "test-requirements": (
            FIXTURES / "test-requirements-generated.md"
        ).read_text(encoding="utf-8"),
        "test-plan": (FIXTURES / "test-plan.md").read_text(encoding="utf-8"),
        "risk.md": (FIXTURES / "risk.md").read_text(encoding="utf-8"),
        "testcases": (FIXTURES / "testcases-valid.md").read_text(encoding="utf-8"),
        "__fix__": (FIXTURES / "testcases-valid.md").read_text(encoding="utf-8"),
        "coverage-matrix": (FIXTURES / "coverage-matrix.md").read_text(encoding="utf-8"),
        "qa-review": (FIXTURES / "qa-review.md").read_text(encoding="utf-8"),
        "__fix_matrix__": (FIXTURES / "coverage-matrix.md").read_text(encoding="utf-8"),
    }


def test_extract_document_strips_fence():
    raw = "```markdown\n# Title\n\nbody\n```"
    assert extract_document(raw) == "# Title\n\nbody"


def test_agent_run_mock(tmp_path, mock_responses):
    out = tmp_path / "output"
    config = QAgentConfig(
        workspace=REPO,
        input_dir=REPO / "input",
        output_dir=out,
        schema_path=REPO / "templates" / "testcase.schema.yaml",
        templates_dir=REPO / "templates",
        retry_limit=2,
        llm=LLMConfig(),
    )
    llm = MockLLM(mock_responses)
    runner = QAgentRunner(config, llm)
    result = runner.run(REPO / "input" / "requirement-example.md")

    assert result.success
    assert result.case_count == 3
    assert (out / "test-requirements.md").is_file()
    assert (out / "test-plan.md").is_file()
    assert (out / "risk.md").is_file()
    assert (out / "testcases.md").is_file()
    assert (out / "testcases.xlsx").is_file()
    assert (out / "coverage-matrix.md").is_file()
    assert (out / "qa-review.md").is_file()
    assert (out / "test-plan-mindmap.md").is_file()
    assert (out / "test-plan.mm").is_file()
    assert (out / "test-plan.opml").is_file()
    assert "R1" in (out / "test-plan-mindmap.md").read_text(encoding="utf-8")
    assert "test_plan_opml" in result.artifacts
    assert "test_requirements" in result.artifacts
    assert "coverage_matrix" in result.artifacts
    assert "qa_review" in result.artifacts
    assert "test_plan_mindmap" in result.artifacts
    assert len(llm.calls) >= 5
