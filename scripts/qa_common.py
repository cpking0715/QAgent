"""向后兼容：委托 qagent.parsing 与 qagent.validation。"""

from qagent.config import resolve_config
from qagent.parsing import parse_cases, parse_requirement_ids, parse_risks, ref_ids
from qagent.schema import load_schema
from qagent.validation import validate_cases, validate_plan_structure, validate_risk_coverage


def validate(cases, requirement_ids, schema=None, config=None):
    """兼容旧 API：validate(cases, requirement_ids) -> (errors, warnings)。"""
    cfg = config or resolve_config()
    sch = schema or load_schema(cfg.schema_path)
    return validate_cases(cases, requirement_ids, sch, cfg)


REQUIRED_FIELDS = None  # 已废弃，请使用 testcase.schema.yaml
ENUMS = None
ID_PATTERN = None

__all__ = [
    "parse_cases",
    "parse_requirement_ids",
    "parse_risks",
    "ref_ids",
    "validate",
    "validate_cases",
    "validate_plan_structure",
    "validate_risk_coverage",
    "load_schema",
]
