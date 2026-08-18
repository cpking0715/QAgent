"""QAgent 校验与解析测试。"""

from pathlib import Path

import pytest

from qagent.config import QAgentConfig, resolve_config
from qagent.exporters import ExportContext, get_exporter
from qagent.parsing import parse_cases, parse_requirement_ids, parse_risks
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
