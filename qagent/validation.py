"""用例、测试方案、风险分析校验。"""

from __future__ import annotations

import re
from pathlib import Path

from qagent.config import QAgentConfig
from qagent.parsing import CoverageRow, ReviewTraceRow, RiskItem, ref_ids
from qagent.schema import TestcaseSchema

MATRIX_CATEGORIES = {"Happy", "Boundary", "Negative", "Security", "State", "Concurrency"}
REVIEW_VERDICTS = {"COVERED", "GAP", "DUPLICATE", "WEAK"}
SC_ID_RE = re.compile(r"^SC-\d{3}$")


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
    covered: set[str] = set()
    for row in rows:
        sid = row.scenario_id
        if not SC_ID_RE.match(sid):
            errors.append(f"{sid}: 场景ID 不符合 SC-NNN")
        if sid in seen:
            errors.append(f"{sid}: 场景ID 重复")
        seen.add(sid)
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
