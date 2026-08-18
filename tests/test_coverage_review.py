from pathlib import Path

import pytest

from qagent.agent.prompts import (
    SYSTEM,
    build_coverage_matrix_prompt,
    build_fix_matrix_prompt,
    build_fix_prompt,
    build_qa_review_prompt,
    build_test_plan_prompt,
    build_testcases_prompt,
)
from qagent.config import QAgentConfig, resolve_config
from qagent.pipeline import PipelineStep, check_prerequisites
from qagent.parsing import (
    ReviewTraceRow,
    _table_after_heading,
    parse_coverage_matrix,
    parse_review_trace,
)
from qagent.validation import validate_matrix, validate_review_trace

FIXTURES = Path(__file__).parent / "fixtures"
REPO = Path(__file__).resolve().parents[1]
SCHEMA = REPO / "templates" / "testcase.schema.yaml"


def _cfg(strict: bool) -> QAgentConfig:
    return QAgentConfig(
        workspace=REPO,
        input_dir=REPO / "input",
        output_dir=REPO / "output",
        schema_path=SCHEMA,
        strict_coverage=strict,
    )


def test_parse_coverage_matrix_valid():
    rows = parse_coverage_matrix(FIXTURES / "coverage-matrix.md")
    assert [r.scenario_id for r in rows] == ["SC-001", "SC-002", "SC-003"]
    assert {r.requirement_id for r in rows} == {"R1", "R2", "R3"}
    assert rows[0].category == "Happy"
    assert rows[0].oracle == "注册成功并可登录"
    assert all(r.scenario_id != "SC-999" for r in rows)


def test_parse_review_trace_valid():
    rows = parse_review_trace(FIXTURES / "qa-review.md")
    assert [r.scenario_id for r in rows] == ["SC-001", "SC-002", "SC-003"]
    assert rows[0].case_id == "TC-REG-001"
    assert rows[0].verdict == "COVERED"


def test_table_after_heading_no_table_stops_at_next_heading():
    """无表格的章节必须在下一标题处结束，不能误解析后续 decoy 表。"""
    md = """\
## 1. 覆盖契约

本节只有文字，没有表格。

## 2. decoy

| 场景ID | 需求ID | 场景 | 类别 | 优先级 | 判定依据 |
| --- | --- | --- | --- | --- | --- |
| SC-999 | R9 | decoy | Happy | P0 | 不应被解析 |
"""
    with pytest.raises(ValueError, match="后没有表格"):
        _table_after_heading(md, "## 1. 覆盖契约")


def test_validate_matrix_rejects_bad_category_and_missing_r():
    rows = parse_coverage_matrix(FIXTURES / "coverage-matrix-bad.md")
    errors, _ = validate_matrix(rows, {"R1", "R2", "R3"}, _cfg(True))
    assert errors
    assert any("Foo" in e or "类别" in e for e in errors)
    assert any("R99" in e for e in errors)


def test_validate_matrix_strict_uncovered_requirement():
    rows = parse_coverage_matrix(FIXTURES / "coverage-matrix.md")
    errors, warnings = validate_matrix(rows, {"R1", "R2", "R3", "R4"}, _cfg(True))
    assert any("R4" in e for e in errors)
    errors2, warnings2 = validate_matrix(rows, {"R1", "R2", "R3", "R4"}, _cfg(False))
    assert not any("R4" in e for e in errors2)
    assert any("R4" in w for w in warnings2)


def test_validate_review_gap_strict():
    rows = parse_review_trace(FIXTURES / "qa-review-gap.md")
    matrix_ids = {"SC-001", "SC-002", "SC-003"}
    case_ids = {"TC-REG-001", "TC-REG-002", "TC-REG-003"}
    errors, _ = validate_review_trace(rows, matrix_ids, case_ids, _cfg(True))
    assert any("GAP" in e or "SC-002" in e for e in errors)


def test_validate_review_covered_unknown_case():
    rows = parse_review_trace(FIXTURES / "qa-review.md")
    rows[0].case_id = "TC-NOPE-001"
    errors, _ = validate_review_trace(
        rows, {"SC-001", "SC-002", "SC-003"},
        {"TC-REG-001", "TC-REG-002", "TC-REG-003"},
        _cfg(True),
    )
    assert any("TC-NOPE-001" in e for e in errors)


def test_validate_review_weak_is_warning():
    rows = [
        ReviewTraceRow("SC-001", "TC-REG-001", "WEAK"),
    ]
    errors, warnings = validate_review_trace(
        rows, {"SC-001"}, {"TC-REG-001"}, _cfg(True),
    )
    assert not errors
    assert warnings


def test_config_artifact_paths(tmp_path):
    cfg = QAgentConfig(
        workspace=REPO, input_dir=tmp_path, output_dir=tmp_path / "out",
        schema_path=SCHEMA,
    )
    assert cfg.coverage_matrix_path == tmp_path / "out" / "coverage-matrix.md"
    assert cfg.qa_review_path == tmp_path / "out" / "qa-review.md"


def test_testcases_requires_matrix(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    cfg = QAgentConfig(
        workspace=REPO, input_dir=tmp_path, output_dir=out, schema_path=SCHEMA,
    )
    (out / "test-requirements.md").write_text("x", encoding="utf-8")
    (out / "test-plan.md").write_text("x", encoding="utf-8")
    (out / "risk.md").write_text("x", encoding="utf-8")
    errors = check_prerequisites(cfg, PipelineStep.TESTCASES)
    assert any("coverage-matrix" in e for e in errors)


def test_prompt_markers_and_signatures():
    cfg = resolve_config(workspace=REPO)
    sys_m, user_m = build_coverage_matrix_prompt("treq", "plan", "risk", cfg)
    assert "生成完整的 coverage-matrix.md" in user_m
    assert "覆盖契约" in user_m
    _, user_fix_m = build_fix_matrix_prompt("matrix", ["SC-001 类别非法"], "plan", cfg)
    assert "coverage-matrix.md" in user_fix_m
    _, user_r = build_qa_review_prompt("matrix", "cases", "plan", "risk", cfg)
    assert "生成完整的 qa-review.md" in user_r
    _, user_plan = build_test_plan_prompt("treq", "src", cfg)
    assert "### 5.1 测试层级" in user_plan
    _, user_tc = build_testcases_prompt("treq", "plan", "risk", "matrix", cfg)
    assert "矩阵" in user_tc
    _, user_fix = build_fix_prompt(
        "cases", ["GAP SC-002"], "plan", cfg,
        test_requirements_text="treq",
        coverage_matrix_text="matrix",
        review_text="review",
    )
    assert "SC-002" in user_fix or "GAP" in user_fix
    assert "矩阵" in SYSTEM or "coverage" in SYSTEM.lower() or "覆盖矩阵" in SYSTEM
