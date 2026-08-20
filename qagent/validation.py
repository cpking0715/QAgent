"""用例、测试方案、风险分析校验。

full_validate 是对全部产物的编排校验（CLI / Runner / 对话修订工具共用），
其余 validate_* 为单项校验原语。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from qagent.config import QAgentConfig
from qagent.parsing import (
    SC_ID_RE,
    CoverageRow,
    ReviewTraceRow,
    RiskItem,
    parse_cases,
    parse_coverage_matrix,
    parse_requirement_ids,
    parse_review_trace,
    parse_risks,
    ref_ids,
)
from qagent.schema import TestcaseSchema, load_schema

MATRIX_CATEGORIES = {"Happy", "Boundary", "Negative", "Security", "State", "Concurrency"}
REVIEW_VERDICTS = {"COVERED", "GAP", "DUPLICATE", "WEAK"}
GAP_EMPTY_CASE_IDS = {"—", "-", "空"}


@dataclass
class ValidateOutcome:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    cases: list[dict] = field(default_factory=list)
    requirement_ids: set[str] = field(default_factory=set)


def full_validate(
    config: QAgentConfig,
    *,
    cases_path: Path | None = None,
    plan_path: Path | None = None,
    risk_path: Path | None = None,
    matrix_path: Path | None = None,
    review_path: Path | None = None,
) -> ValidateOutcome:
    """按 config（可用路径参数覆盖）校验全部产物，聚合 errors/warnings。

    路径缺省时取 config 的产物路径；文件不存在时按可选项跳过对应校验。
    """
    outcome = ValidateOutcome()
    cases_path = cases_path or config.testcases_path
    plan_path = plan_path or config.test_plan_path
    risk_path = risk_path or config.risk_path
    matrix_path = matrix_path or config.coverage_matrix_path
    review_path = review_path or config.qa_review_path
    schema = load_schema(config.schema_path)

    try:
        outcome.cases = parse_cases(cases_path)
    except ValueError as exc:
        outcome.errors.append(str(exc))
        return outcome

    if plan_path.is_file():
        outcome.errors.extend(validate_plan_structure(plan_path, schema))

    outcome.requirement_ids = (
        parse_requirement_ids(plan_path) if plan_path.is_file() else set()
    )
    case_errors, case_warnings = validate_cases(
        outcome.cases, outcome.requirement_ids, schema, config,
    )
    outcome.errors.extend(case_errors)
    outcome.warnings.extend(case_warnings)

    if risk_path.is_file():
        try:
            risks = parse_risks(risk_path)
            risk_errors, risk_warnings = validate_risk_coverage(
                outcome.cases, risks, schema,
            )
            outcome.errors.extend(risk_errors)
            outcome.warnings.extend(risk_warnings)
        except ValueError as exc:
            outcome.errors.append(f"risk.md 解析失败: {exc}")

    if not matrix_path.is_file():
        outcome.errors.append(f"缺少文件: {matrix_path}")
    if not review_path.is_file():
        outcome.errors.append(f"缺少文件: {review_path}")
    if matrix_path.is_file() and review_path.is_file() and plan_path.is_file():
        try:
            matrix_rows = parse_coverage_matrix(matrix_path)
            review_rows = parse_review_trace(review_path)
            m_err, m_warn = validate_matrix(matrix_rows, outcome.requirement_ids, config)
            outcome.errors.extend(m_err)
            outcome.warnings.extend(m_warn)
            case_ids = {str(c.get("id")) for c in outcome.cases if c.get("id")}
            r_err, r_warn = validate_review_trace(
                review_rows,
                {row.scenario_id for row in matrix_rows},
                case_ids,
                config,
            )
            outcome.errors.extend(r_err)
            outcome.warnings.extend(r_warn)
        except ValueError as exc:
            outcome.errors.append(str(exc))

    return outcome


def validate_cases(
    cases: list[dict],
    requirement_ids: set[str],
    schema: TestcaseSchema,
    config: QAgentConfig | None = None,
) -> tuple[list[str], list[str]]:
    """返回 (errors, warnings)。"""
    errors: list[str] = []
    warnings: list[str] = []
    seen_ids: set[str] = set()
    strict = config.strict_coverage if config else False

    if not cases:
        errors.append("testcases.md 中没有任何 yaml 用例块")
        return errors, warnings

    covered: set[str] = set()
    for idx, case in enumerate(cases, start=1):
        label = case.get("id") or f"第{idx}条"

        for field_name in schema.required_fields:
            value = case.get(field_name)
            if value in (None, "", []):
                errors.append(f"{label}: 缺少必填字段 {field_name}")

        for field_name, allowed in schema.enum_fields.items():
            value = case.get(field_name)
            if value is not None and str(value) not in allowed:
                errors.append(
                    f"{label}: {field_name}={value!r} 不在枚举 {sorted(allowed)} 内")

        case_id = case.get("id")
        if case_id and schema.id_pattern:
            if not schema.id_pattern.match(str(case_id)):
                errors.append(f"{label}: id 不符合 TC-<模块缩写>-<3位序号> 格式")
            if case_id in seen_ids:
                errors.append(f"{label}: id 重复")
            seen_ids.add(case_id)

        for field_name in schema.list_fields:
            value = case.get(field_name)
            if value is not None and not isinstance(value, list):
                errors.append(f"{label}: {field_name} 必须是列表")

        refs = ref_ids(case)
        if refs:
            unknown = [r for r in refs if r not in requirement_ids]
            if unknown:
                errors.append(
                    f"{label}: requirement_ref 引用了不存在的需求 {unknown}，"
                    f"可用需求 ID: {sorted(requirement_ids)}")
            covered.update(r for r in refs if r in requirement_ids)

    uncovered = requirement_ids - covered
    if uncovered:
        message = f"以下需求条目没有用例覆盖: {sorted(uncovered)}"
        if strict:
            errors.append(message)
        else:
            warnings.append(message)

    return errors, warnings


def validate_matrix(
    rows: list[CoverageRow],
    requirement_ids: set[str],
    config: QAgentConfig | None = None,
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    strict = config.strict_coverage if config else False
    if not rows:
        return ["覆盖矩阵没有任何场景行"], warnings

    seen: set[str] = set()
    seen_scenarios: set[str] = set()
    covered: set[str] = set()
    for row in rows:
        sid = row.scenario_id
        if not SC_ID_RE.fullmatch(sid):
            errors.append(f"{sid}: 场景ID 不符合 SC-NNN")
        if sid in seen:
            errors.append(f"{sid}: 场景ID 重复")
        seen.add(sid)
        scenario_text = row.scenario.strip()
        if not scenario_text:
            errors.append(f"{sid}: 场景不能为空")
        elif scenario_text in seen_scenarios:
            errors.append(f"{sid}: 场景文本重复: {scenario_text}")
        else:
            seen_scenarios.add(scenario_text)
        if row.category not in MATRIX_CATEGORIES:
            errors.append(f"{sid}: 类别 {row.category!r} 不合法")
        if row.priority not in {"P0", "P1", "P2"}:
            errors.append(f"{sid}: 优先级 {row.priority!r} 不合法")
        if row.requirement_id not in requirement_ids:
            errors.append(f"{sid}: 需求 {row.requirement_id} 不存在")
        else:
            covered.add(row.requirement_id)

    for rid in sorted(requirement_ids - covered):
        msg = f"需求 {rid} 在覆盖矩阵中没有场景行"
        if strict:
            errors.append(msg)
        else:
            warnings.append(msg)
    return errors, warnings


def validate_review_trace(
    rows: list[ReviewTraceRow],
    matrix_ids: set[str],
    case_ids: set[str],
    config: QAgentConfig | None = None,
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    strict = config.strict_coverage if config else False
    present = {r.scenario_id for r in rows}

    for sid in sorted(matrix_ids - present):
        errors.append(f"{sid}: 追溯表缺失")
    for row in rows:
        if row.scenario_id not in matrix_ids:
            errors.append(f"{row.scenario_id}: 追溯表出现未知场景")
        if row.verdict not in REVIEW_VERDICTS:
            errors.append(f"{row.scenario_id}: 结论 {row.verdict!r} 不合法")
            continue
        if row.verdict == "GAP":
            if row.case_id.strip() not in GAP_EMPTY_CASE_IDS:
                errors.append(
                    f"{row.scenario_id}: 结论为 GAP 时对应用例必须为空"
                    f"（— / - / 空），实际 {row.case_id}"
                )
            msg = f"{row.scenario_id}: 结论为 GAP"
            if strict:
                errors.append(msg)
            else:
                warnings.append(msg)
            continue
        if row.case_id not in case_ids:
            errors.append(f"{row.scenario_id}: 用例 {row.case_id} 不存在")
        if row.verdict == "WEAK":
            warnings.append(f"{row.scenario_id}: 结论为 WEAK")
    return errors, warnings


def validate_plan_structure(plan_path: Path, schema: TestcaseSchema) -> list[str]:
    """校验 test-plan.md 必填章节与 requirements 块。"""
    errors: list[str] = []
    text = plan_path.read_text(encoding="utf-8")

    for section in schema.plan_required_sections:
        if section not in text:
            errors.append(f"test-plan.md 缺少章节: {section}")

    if "```requirements" not in text:
        errors.append("test-plan.md 缺少 ```requirements 代码块")

    return errors


def validate_risk_coverage(
    cases: list[dict],
    risks: list[RiskItem],
    schema: TestcaseSchema,
) -> tuple[list[str], list[str]]:
    """校验 CRITICAL/HIGH 风险是否有对应用例优先级覆盖。"""
    errors: list[str] = []
    warnings: list[str] = []

    if not risks:
        warnings.append("risk.md 未解析到任何 RK 风险项")
        return errors, warnings

    for risk in risks:
        zone_cfg = schema.risk_zones.get(risk.zone, {})
        required_priorities = set(zone_cfg.get("required_priorities") or [])
        if not required_priorities:
            continue

        related_cases = [
            case for case in cases
            if risk.requirement_refs
            and any(ref in ref_ids(case) for ref in risk.requirement_refs)
        ]
        if not related_cases and not risk.requirement_refs:
            related_cases = [
                case for case in cases
                if str(case.get("priority")) == risk.case_priority
            ]

        priorities = {str(case.get("priority")) for case in related_cases}
        if not priorities.intersection(required_priorities):
            errors.append(
                f"{risk.risk_id} ({risk.zone}): 需要 {sorted(required_priorities)} "
                f"用例，当前关联用例优先级 {sorted(priorities) or ['无']}")

    return errors, warnings
