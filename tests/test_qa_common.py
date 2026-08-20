"""QAgent 校验与解析测试。"""

from pathlib import Path

import pytest

from qagent.config import QAgentConfig, resolve_config
from qagent.exporters import ExportContext, get_exporter
from qagent.agent.runner import keep_one_case_per_row
from qagent.exporters.mindmap import write_test_plan_mindmaps
from qagent.parsing import (
    CoverageRow,
    fill_missing_cases,
    normalize_case,
    parse_coverage_matrix_text,
    render_coverage_matrix_md,
    render_qa_review_md,
    renumber_matrix_rows,
    parse_cases,
    parse_cases_text,
    parse_requirement_ids,
    parse_risks,
    sanitize_requirement_ref,
)
from qagent.schema import load_schema
from qagent.validation import validate_cases, validate_plan_structure, validate_risk_coverage

FIXTURES = Path(__file__).parent / "fixtures"
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "templates" / "testcase.schema.yaml"


@pytest.fixture
def schema():
    return load_schema(SCHEMA_PATH)


@pytest.fixture
def requirement_ids():
    return parse_requirement_ids(FIXTURES / "test-plan.md")


def test_parse_requirement_ids(requirement_ids):
    assert requirement_ids == {"R1", "R2", "R3"}


def test_parse_cases_unclosed_fence():
    text = """# 截断稿

```yaml
id: TC-REG-001
title: 正确注册
priority: P0
type: 功能
preconditions: []
steps:
  - 输入未注册手机号
expected: 注册成功
design_method: 等价类
requirement_ref: R1
"""
    cases = parse_cases_text(text)
    assert len(cases) == 1
    assert cases[0]["id"] == "TC-REG-001"


def test_parse_cases_yaml_list_block():
    text = """```yaml
- id: TC-A-001
  title: a
  requirement_ref: R1
- id: TC-A-002
  title: b
  requirement_ref: R2
```
"""
    cases = parse_cases_text(text)
    assert [c["id"] for c in cases] == ["TC-A-001", "TC-A-002"]


def test_parse_cases_llm_backticks_and_json():
    text = """```yaml
id: TC-API-001
title: SC-001 状态查询
steps:
  - `status` 字段为 `PROCESSING`
  - 请求体JSON: `{"type": "CARD", "page": 1}`
  - 请求体为：`{"result": {"姓名": "王五"}}`。
expected: 调用 GET /template/list` 参数中包含 `type: "卡证"`。
requirement_ref: R1
```
"""
    cases = parse_cases_text(text)
    assert len(cases) == 1
    assert cases[0]["id"] == "TC-API-001"
    assert any("PROCESSING" in str(s) for s in cases[0]["steps"])
    assert "卡证" in str(cases[0]["expected"])


def test_parse_cases_skips_one_bad_block():
    text = """```yaml
id: TC-A-001
title: ok
requirement_ref: R1
```

```yaml
[1, 2, 3
```

```yaml
id: TC-A-002
title: also ok
requirement_ref: R2
```
"""
    cases = parse_cases_text(text)
    assert [c["id"] for c in cases] == ["TC-A-001", "TC-A-002"]


def test_keep_one_case_per_row_drops_extras():
    rows = [
        CoverageRow("SC-001", "R1", "注册", "Happy", "P0", "成功"),
        CoverageRow("SC-002", "R2", "过期", "Boundary", "P1", "过期"),
    ]
    cases = [
        {"id": "TC-A-001", "title": "SC-001 正确注册", "requirement_ref": "R1"},
        {"id": "TC-A-002", "title": "SC-001 再测一次", "requirement_ref": "R1"},
        {"id": "TC-A-003", "title": "验证码超时", "requirement_ref": "R2"},
    ]
    kept = keep_one_case_per_row(cases, rows)
    assert [c["id"] for c in kept] == ["TC-A-001", "TC-A-003"]
    assert kept[1]["title"].startswith("SC-002")


def test_normalize_case_coerces_schema_fields():
    case = {
        "id": "bad-id",
        "title": "SC-009 状态查询",
        "priority": "HIGH",
        "type": "状态转换",
        "steps": "1. 打开页面。 2. 点击提交。",
        "expected": ["返回 200", "列表可见"],
        "design_method": "组合测试",
        "requirement_ref": "SC-009,R2",
    }
    normalize_case(case, {"R1", "R2"}, {"SC-009": "R2"})
    assert case["id"] == "TC-SC-009"
    assert case["type"] == "功能"
    assert case["design_method"] == "pairwise"
    assert case["priority"] == "P1"
    assert case["steps"] == ["打开页面", "点击提交"]
    assert "200" in case["expected"]
    assert case["requirement_ref"] == "R2"


def test_normalize_case_infers_perf_requirement():
    case = {
        "id": "TC-PERF-001",
        "title": "简单图片识别性能 SLA",
        "priority": "P1",
        "type": "性能",
        "steps": ["上传100字图片并计时"],
        "expected": "4秒内返回结果",
        "design_method": "场景法",
    }
    items = [
        ("R1", "上传 JPG 图片并识别"),
        ("R28", "简单文本图片识别速度 ≤ 4秒"),
    ]
    normalize_case(case, {"R1", "R28"}, req_items=items)
    assert case["requirement_ref"] == "R28"
    assert case["type"] == "功能"


def test_normalize_case_fills_missing_ref_with_first_r():
    case = {
        "id": "TC-X-001",
        "title": "补充说明",
        "steps": ["打开页面"],
        "expected": "可见",
        "type": "功能",
        "design_method": "场景法",
        "priority": "P1",
    }
    normalize_case(case, {"R3", "R1"})
    assert case["requirement_ref"] == "R1"


def test_fill_missing_and_script_review():
    rows = [
        CoverageRow("SC-001", "R1", "注册", "Happy", "P0", "成功"),
        CoverageRow("SC-002", "R2", "过期", "Boundary", "P1", "过期"),
    ]
    cases = [{"id": "TC-A-001", "title": "SC-001 正确注册", "requirement_ref": "R1"}]
    filled = fill_missing_cases(cases, rows)
    assert [c["id"] for c in filled] == ["TC-A-001", "TC-SC-002"]
    review = render_qa_review_md(rows, filled)
    assert "| SC-001 | TC-A-001 | COVERED |" in review
    assert "| SC-002 | TC-SC-002 | COVERED |" in review
    assert "GAP" not in review.split("## 1.")[1].split("## 2.")[0]


def test_renumber_matrix_rows():
    text = render_coverage_matrix_md([
        CoverageRow("SC-001", "R1", "a", "Happy", "P0", "x"),
        CoverageRow("SC-001", "R2", "b", "Boundary", "P1", "y"),
    ])
    rows = renumber_matrix_rows(parse_coverage_matrix_text(text))
    assert [r.scenario_id for r in rows] == ["SC-001", "SC-002"]
    assert [r.requirement_id for r in rows] == ["R1", "R2"]


def test_sanitize_requirement_ref():
    valid = {"R1", "R2"}
    assert sanitize_requirement_ref("R1,SC-001,F1", valid) == "R1"
    assert sanitize_requirement_ref("SC-001", valid) == ""
    assert sanitize_requirement_ref("R9", valid) == "R9"


def test_mindmap_contains_requirements(tmp_path):
    md = tmp_path / "test-plan-mindmap.md"
    mm = tmp_path / "test-plan.mm"
    write_test_plan_mindmaps(
        plan_path=FIXTURES / "test-plan.md",
        md_path=md,
        mm_path=mm,
        matrix_path=FIXTURES / "coverage-matrix.md",
        risk_path=FIXTURES / "risk.md",
    )
    outline = md.read_text(encoding="utf-8")
    freemind = mm.read_text(encoding="utf-8")
    opml = mm.with_suffix(".opml").read_text(encoding="utf-8")
    assert "R1" in outline
    assert "SC-001" in outline
    assert "<map" in freemind
    assert "RK1" in freemind
    assert "<opml" in opml
    assert 'text="R1' in opml or "R1" in opml


def test_nested_markdown_list_to_opml():
    from qagent.exporters.mindmap import markdown_to_opml, parse_markdown_outline

    tree = parse_markdown_outline("- 根节点\n  - 子A\n  - 子B\n    - 孙\n")
    assert tree["text"] == "根节点"
    assert [c["text"] for c in tree["children"]] == ["子A", "子B"]
    assert tree["children"][1]["children"][0]["text"] == "孙"
    xml = markdown_to_opml("# 方案\n\n> 忽略这行\n\n## 范围\n- 上传\n  - JPG\n")
    assert "<opml version=\"2.0\">" in xml
    assert 'text="方案"' in xml
    assert 'text="范围"' in xml
    assert 'text="JPG"' in xml
    escaped = markdown_to_opml("- A & B\n")
    assert "A &amp; B" in escaped


def test_cli_mindmap_converts_nested_list(tmp_path):
    src = tmp_path / "outline.md"
    src.write_text("- 根\n  - 子\n", encoding="utf-8")
    dest = tmp_path / "outline.opml"
    from qagent.cli import main
    assert main(["mindmap", str(src), "-o", str(dest)]) == 0
    text = dest.read_text(encoding="utf-8")
    assert "<opml" in text
    assert 'text="根"' in text
    assert 'text="子"' in text


def test_valid_cases_pass(schema, requirement_ids):
    cases = parse_cases(FIXTURES / "testcases-valid.md")
    errors, warnings = validate_cases(cases, requirement_ids, schema)
    assert not errors
    assert not warnings


@pytest.mark.parametrize(
    "fixture",
    [
        "testcases-bad-enum.md",
        "testcases-bad-dup.md",
        "testcases-bad-ref.md",
        "testcases-bad-missing.md",
        "testcases-bad-id.md",
        "testcases-bad-type.md",
    ],
)
def test_negative_fixtures_fail(schema, requirement_ids, fixture):
    cases = parse_cases(FIXTURES / fixture)
    errors, _ = validate_cases(cases, requirement_ids, schema)
    assert errors


def test_strict_coverage(schema, requirement_ids):
    cases = parse_cases(FIXTURES / "testcases-valid.md")
    cases = [c for c in cases if c["requirement_ref"] != "R2"]
    config = QAgentConfig(
        workspace=FIXTURES.parents[1],
        input_dir=FIXTURES,
        output_dir=FIXTURES,
        strict_coverage=True,
        schema_path=SCHEMA_PATH,
    )
    errors, warnings = validate_cases(cases, requirement_ids, schema, config)
    assert errors
    assert any("R2" in e for e in errors)


def test_parse_risks(schema):
    risks = parse_risks(FIXTURES / "risk.md")
    assert len(risks) == 2
    assert risks[0].zone == "CRITICAL"
    assert risks[0].requirement_refs == ["R3"]


def test_risk_coverage_pass(schema):
    cases = parse_cases(FIXTURES / "testcases-valid.md")
    risks = parse_risks(FIXTURES / "risk.md")
    errors, _ = validate_risk_coverage(cases, risks, schema)
    assert not errors


def test_risk_coverage_fail(schema):
    cases = parse_cases(FIXTURES / "testcases-valid.md")
    cases = [c for c in cases if c["id"] != "TC-REG-002"]
    risks = parse_risks(FIXTURES / "risk.md")
    errors, _ = validate_risk_coverage(cases, risks, schema)
    assert errors


def test_plan_structure(schema):
    errors = validate_plan_structure(FIXTURES / "test-plan.md", schema)
    assert not errors


def test_export_xlsx(schema, tmp_path):
    cases = parse_cases(FIXTURES / "testcases-valid.md")
    out = tmp_path / "cases.xlsx"
    exporter = get_exporter("xlsx")
    path = exporter.export(ExportContext(output_path=out, schema=schema, cases=cases))
    assert path.is_file()
    assert path.stat().st_size > 0


def test_resolve_config_uses_repo_schema():
    config = resolve_config(workspace=Path(__file__).resolve().parents[1])
    assert config.schema_path.name == "testcase.schema.yaml"
    assert config.schema_path.is_file()
