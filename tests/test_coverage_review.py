from pathlib import Path

import pytest

from qagent.parsing import _table_after_heading, parse_coverage_matrix, parse_review_trace

FIXTURES = Path(__file__).parent / "fixtures"


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
