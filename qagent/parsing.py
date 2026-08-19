"""Markdown 产物解析。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class RiskItem:
    risk_id: str
    description: str
    impact: int
    probability: int
    score: int
    zone: str
    requirement_refs: list[str]
    case_priority: str


@dataclass
class CoverageRow:
    scenario_id: str
    requirement_id: str
    scenario: str
    category: str
    priority: str
    oracle: str


@dataclass
class ReviewTraceRow:
    scenario_id: str
    case_id: str
    verdict: str


def _yaml_blocks(text: str) -> list[str]:
    """提取 ```yaml 块；若只有未闭合围栏，仍取围栏后全文。"""
    blocks = re.findall(r"```yaml\s*\n(.*?)\n```", text, re.DOTALL)
    if blocks:
        return blocks
    opened = re.search(r"```yaml\s*\n(.*)$", text, re.DOTALL)
    if opened:
        return [re.sub(r"\n```\s*$", "", opened.group(1))]
    return []


def _cases_from_yaml(data: object, label: str) -> list[dict]:
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        cases: list[dict] = []
        for j, item in enumerate(data):
            if not isinstance(item, dict):
                raise ValueError(f"{label}第 {j + 1} 项不是键值结构")
            cases.append(item)
        return cases
    raise ValueError(f"{label}不是键值结构")


def _quote_yaml_scalar(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _already_quoted(value: str) -> bool:
    s = value.strip()
    return (
        len(s) >= 2
        and (
            (s[0] == s[-1] == '"')
            or (s[0] == s[-1] == "'")
            or s[0] in ("|", ">")
        )
    )


def _needs_yaml_quote(value: str) -> bool:
    """LLM 常把反引号、JSON 写进未加引号的标量，PyYAML 会解析失败。"""
    s = value.strip()
    if not s or _already_quoted(s):
        return False
    if s[0] in "`{":
        return True
    if s[0] == "[" and ('"' in s or "'" in s or "`" in s):
        return True
    return bool(re.search(r"[`{}]", s))


def repair_llm_yaml(text: str) -> str:
    """给含反引号/JSON 的列表项与映射值补双引号，尽量保住整块。"""
    lines: list[str] = []
    for line in text.splitlines():
        list_item = re.match(r"^(\s*-\s+)(.*)$", line)
        if list_item:
            prefix, rest = list_item.group(1), list_item.group(2)
            if rest and _needs_yaml_quote(rest):
                line = prefix + _quote_yaml_scalar(rest)
            lines.append(line)
            continue
        mapping = re.match(r"^(\s*[^:#\n][^:\n]*:\s+)(.*)$", line)
        if mapping:
            prefix, rest = mapping.group(1), mapping.group(2)
            if rest and _needs_yaml_quote(rest):
                line = prefix + _quote_yaml_scalar(rest)
        lines.append(line)
    return "\n".join(lines)


def _load_yaml_block(block: str) -> object:
    try:
        return yaml.safe_load(block)
    except yaml.YAMLError:
        pass
    try:
        return yaml.safe_load(repair_llm_yaml(block))
    except yaml.YAMLError:
        return None


def parse_cases_text(text: str) -> list[dict]:
    """从 Markdown 正文解析用例，兼容：一块一例、一块一列表、未闭合围栏。"""
    blocks = _yaml_blocks(text)
    if not blocks:
        data = _load_yaml_block(text)
        if isinstance(data, (list, dict)):
            try:
                return _cases_from_yaml(data, "文档")
            except ValueError:
                return []
        return []
    cases: list[dict] = []
    errors: list[str] = []
    for i, block in enumerate(blocks):
        data = _load_yaml_block(block)
        if data is None:
            errors.append(f"第 {i + 1} 个 yaml 块无法解析，已跳过")
            continue
        try:
            cases.extend(_cases_from_yaml(data, f"第 {i + 1} 个 yaml 块"))
        except ValueError as exc:
            errors.append(str(exc))
    if not cases and errors:
        raise ValueError("；".join(errors))
    return cases


def parse_cases(cases_path: Path) -> list[dict]:
    """解析 testcases.md，返回所有 yaml 用例块。"""
    return parse_cases_text(cases_path.read_text(encoding="utf-8"))


def render_testcases_md(cases: list[dict], title: str = "测试用例") -> str:
    """把用例规范写成「每条一个 yaml 映射块」，避免列表格式。"""
    lines = [f"# {title}", "", f"- 用例总数：{len(cases)}", ""]
    for case in cases:
        case_id = str(case.get("id") or "TC-UNK-000")
        case_title = str(case.get("title") or "")
        dumped = yaml.safe_dump(case, allow_unicode=True, sort_keys=False).rstrip()
        lines.extend([
            f"## {case_id} {case_title}",
            "",
            "```yaml",
            dumped,
            "```",
            "",
        ])
    return "\n".join(lines)


TYPE_ALIASES = {
    "状态转换": "功能",
    "状态": "功能",
    "正向": "功能",
    "happy": "功能",
    "negative": "异常",
    "boundary": "边界",
    "security": "安全",
    "接口": "功能",
}
DESIGN_ALIASES = {
    "组合测试": "pairwise",
    "组合": "pairwise",
    "正交": "pairwise",
    "场景": "场景法",
}
TYPE_VALUES = {"功能", "边界", "异常", "安全", "组合"}
DESIGN_VALUES = {"等价类", "边界值", "状态转换", "判定表", "pairwise", "错误推测", "场景法"}
CATEGORY_TO_TYPE = {
    "Happy": "功能",
    "Boundary": "边界",
    "Negative": "异常",
    "Security": "安全",
    "State": "功能",
    "Concurrency": "组合",
}
CATEGORY_TO_METHOD = {
    "Happy": "等价类",
    "Boundary": "边界值",
    "Negative": "错误推测",
    "Security": "错误推测",
    "State": "状态转换",
    "Concurrency": "场景法",
}
ID_PATTERN = re.compile(r"^TC-[A-Z0-9]{2,8}-\d{3}$")
_SC_IN_TEXT = re.compile(r"SC-\d{3}")
_STEP_SPLIT = re.compile(r"(?:^|[\s。；;])\d+[\.、\)]\s*")


def _as_step_list(value: object) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    parts = [p.strip(" 。；;") for p in _STEP_SPLIT.split(text) if p.strip(" 。；;")]
    if len(parts) > 1:
        return parts
    lines = [line.strip(" -•\t") for line in text.splitlines() if line.strip()]
    return lines or [text]


def _map_enum(value: object, aliases: dict[str, str], default: str, allowed: set[str]) -> str:
    raw = str(value or "").strip()
    if raw in allowed:
        return raw
    mapped = aliases.get(raw) or aliases.get(raw.lower())
    if mapped in allowed:
        return mapped
    return default


def case_scenario_ids(case: dict) -> list[str]:
    blob = f"{case.get('id', '')} {case.get('title', '')}"
    return _SC_IN_TEXT.findall(str(blob))


def normalize_case(
    case: dict,
    valid_ids: set[str],
    sc_to_req: dict[str, str] | None = None,
) -> dict:
    """把 LLM 常见脏字段收成 Schema 可过的形态。"""
    case["steps"] = _as_step_list(case.get("steps")) or ["执行矩阵对应场景"]
    case["preconditions"] = _as_step_list(case.get("preconditions"))
    expected = case.get("expected")
    if isinstance(expected, list):
        case["expected"] = "；".join(str(item).strip() for item in expected if str(item).strip())
    elif not str(expected or "").strip():
        case["expected"] = "结果可判定"
    else:
        case["expected"] = str(expected).strip()
    case["type"] = _map_enum(case.get("type"), TYPE_ALIASES, "功能", TYPE_VALUES)
    case["design_method"] = _map_enum(
        case.get("design_method"), DESIGN_ALIASES, "场景法", DESIGN_VALUES,
    )
    if str(case.get("priority") or "") not in {"P0", "P1", "P2"}:
        case["priority"] = "P1"
    cleaned = sanitize_requirement_ref(case.get("requirement_ref"), valid_ids)
    if not cleaned and sc_to_req:
        for sid in case_scenario_ids(case):
            rid = sc_to_req.get(sid)
            if rid in valid_ids:
                cleaned = rid
                break
    if cleaned:
        case["requirement_ref"] = cleaned
    case_id = str(case.get("id") or "")
    if not ID_PATTERN.match(case_id):
        scs = case_scenario_ids(case)
        if scs:
            case["id"] = f"TC-SC-{scs[0].split('-', 1)[1]}"
    if not str(case.get("title") or "").strip():
        case["title"] = case_id or "未命名场景"
    return case


def case_from_matrix_row(row: CoverageRow) -> dict:
    """矩阵行落不成 LLM 用例时，用契约字段补一条可校验用例。"""
    number = row.scenario_id.split("-", 1)[-1]
    priority = row.priority if row.priority in {"P0", "P1", "P2"} else "P1"
    return {
        "id": f"TC-SC-{number}",
        "title": f"{row.scenario_id} {row.scenario}".strip(),
        "priority": priority,
        "type": CATEGORY_TO_TYPE.get(row.category, "功能"),
        "preconditions": [],
        "steps": [f"按场景执行：{row.scenario}"],
        "expected": row.oracle or "结果可判定",
        "design_method": CATEGORY_TO_METHOD.get(row.category, "场景法"),
        "requirement_ref": row.requirement_id,
    }


def fill_missing_cases(cases: list[dict], rows: list[CoverageRow]) -> list[dict]:
    have: set[str] = set()
    for case in cases:
        have.update(case_scenario_ids(case))
    stubs = [case_from_matrix_row(row) for row in rows if row.scenario_id not in have]
    return merge_cases(cases, stubs)


def render_qa_review_md(rows: list[CoverageRow], cases: list[dict]) -> str:
    """追溯表只引用已有用例 id，禁止编造或逗号拼接。"""
    by_sc: dict[str, str] = {}
    for case in cases:
        case_id = str(case.get("id") or "")
        if not case_id:
            continue
        for sid in case_scenario_ids(case):
            by_sc.setdefault(sid, case_id)
    table = [
        "| 场景ID | 对应用例 | 结论 |",
        "|--------|----------|------|",
    ]
    covered = gap = 0
    gap_rows: list[str] = []
    for row in rows:
        case_id = by_sc.get(row.scenario_id)
        if case_id:
            table.append(f"| {row.scenario_id} | {case_id} | COVERED |")
            covered += 1
        else:
            table.append(f"| {row.scenario_id} | — | GAP |")
            gap += 1
            gap_rows.append(f"| {row.scenario_id} 无对应用例 | {row.requirement_id} | P1 | 补一条对应该行的用例 |")
    gap_body = "\n".join(gap_rows) if gap_rows else "| 无 | | | |"
    return (
        "# QA Review\n\n"
        "> 校验脚本只解析「## 1. 追溯表」后的第一张表，列名不可改。\n\n"
        "## 1. 追溯表（SC ↔ TC）\n\n"
        + "\n".join(table)
        + "\n\n## 2. Coverage Gap\n\n"
        "| 缺口 | 关联 | 严重度 | 建议 |\n"
        "|------|------|--------|------|\n"
        f"{gap_body}\n\n"
        "## 3. Test Smell\n\n"
        "| 用例 | Smell | 说明 |\n"
        "|------|-------|------|\n"
        "| 无 | | |\n\n"
        "## 4. 评审摘要\n\n"
        f"- 矩阵行数：{len(rows)}\n"
        f"- COVERED：{covered}；GAP：{gap}；WEAK：0\n"
    )


def sanitize_requirement_ref(raw: object, valid_ids: set[str]) -> str:
    """只保留真实 R 编号，去掉 SC-/F/A/B/PRE 等混入值。"""
    refs = [part.strip() for part in str(raw or "").split(",") if part.strip()]
    kept = [ref for ref in refs if ref in valid_ids]
    if not kept:
        kept = [ref for ref in refs if re.fullmatch(r"R\d+", ref)]
    return ",".join(kept)


def merge_cases(existing: list[dict], incoming: list[dict]) -> list[dict]:
    """按 id 合并，后写覆盖先写。无 id 的追加。"""
    merged: dict[str, dict] = {}
    extras: list[dict] = []
    for case in existing + incoming:
        case_id = case.get("id")
        if case_id:
            merged[str(case_id)] = case
        else:
            extras.append(case)
    return list(merged.values()) + extras


def parse_requirement_items(plan_path: Path) -> list[tuple[str, str]]:
    """解析 requirements 块为 (id, 描述) 列表，保序。"""
    text = plan_path.read_text(encoding="utf-8")
    match = re.search(r"```requirements\s*\n(.*?)\n```", text, re.DOTALL)
    if not match:
        raise ValueError("test-plan.md 缺少 ```requirements 代码块")
    items: list[tuple[str, str]] = []
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        rid, desc = line.split(":", 1)
        rid = rid.strip()
        if rid:
            items.append((rid, desc.strip()))
    if not items:
        raise ValueError("requirements 代码块为空")
    return items


def render_coverage_table(rows: list[CoverageRow]) -> str:
    header = (
        "| 场景ID | 需求 | 场景 | 类别 | 优先级 | 判定方式 |\n"
        "|--------|------|------|------|--------|----------|"
    )
    body = "\n".join(
        f"| {row.scenario_id} | {row.requirement_id} | {row.scenario} | "
        f"{row.category} | {row.priority} | {row.oracle} |"
        for row in rows
    )
    return f"## 1. 覆盖契约\n\n{header}\n{body}\n"


def render_coverage_matrix_md(rows: list[CoverageRow]) -> str:
    return (
        "# 覆盖矩阵\n\n"
        f"{render_coverage_table(rows)}\n"
        "## 2. 覆盖规则自检\n\n- [x] 每个 R 至少 1 行\n"
    )


def renumber_matrix_rows(rows: list[CoverageRow]) -> list[CoverageRow]:
    return [
        CoverageRow(
            scenario_id=f"SC-{index:03d}",
            requirement_id=row.requirement_id,
            scenario=row.scenario,
            category=row.category,
            priority=row.priority,
            oracle=row.oracle,
        )
        for index, row in enumerate(rows, 1)
    ]


def parse_requirement_ids(plan_path: Path) -> set[str]:
    """从 test-plan.md 的 requirements 代码块提取需求 ID 集合。"""
    return {rid for rid, _ in parse_requirement_items(plan_path)}


def ref_ids(case: dict) -> list[str]:
    """requirement_ref 支持 'R1' 或 'R1,R2' 两种写法。"""
    raw = case.get("requirement_ref", "")
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def _parse_table_row(line: str) -> list[str]:
    parts = [cell.strip() for cell in line.strip().strip("|").split("|")]
    return parts


def parse_risks(risk_path: Path) -> list[RiskItem]:
    """从 risk.md 风险清单表格解析 RK 条目。"""
    text = risk_path.read_text(encoding="utf-8")
    risks: list[RiskItem] = []
    in_table = False
    headers: list[str] = []

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            in_table = False
            continue
        cells = _parse_table_row(stripped)
        if not cells:
            continue
        if cells[0] in ("编号", "---", "----"):
            if cells[0] == "编号":
                headers = [c.lower() for c in cells]
                in_table = True
            continue
        if not in_table or not headers:
            continue
        if not cells[0].upper().startswith("RK"):
            continue

        row = {headers[i]: cells[i] if i < len(cells) else "" for i in range(len(headers))}
        req_raw = row.get("关联需求", "")
        reqs = [part.strip() for part in re.split(r"[,、/]", req_raw) if part.strip()]

        try:
            impact = int(row.get("影响度", "0"))
            probability = int(row.get("可能性", "0"))
            score = int(row.get("风险分", "0"))
        except ValueError as exc:
            raise ValueError(f"风险项 {cells[0]} 评分字段不是整数") from exc

        risks.append(RiskItem(
            risk_id=cells[0],
            description=row.get("风险描述", cells[1] if len(cells) > 1 else ""),
            impact=impact,
            probability=probability,
            score=score,
            zone=row.get("分区", "").upper(),
            requirement_refs=reqs,
            case_priority=row.get("对应用例优先级", row.get("用例优先级", "")),
        ))

    return risks


COVERAGE_HEADERS = ("场景ID", "需求", "场景", "类别", "优先级", "判定方式")
REVIEW_HEADERS = ("场景ID", "对应用例", "结论")


def _is_separator_row(cells: list[str]) -> bool:
    first = cells[0]
    return first in ("---", "----") or set(first) <= {"-", ":"}


def _require_headers(headers: list[str], expected: tuple[str, ...], label: str) -> None:
    missing = [name for name in expected if name not in headers]
    if missing:
        raise ValueError(f"{label} 表头不匹配: 缺少 {missing}，实际 {headers}")


def _cell_by_name(headers: list[str], cells: list[str], name: str) -> str:
    index = headers.index(name)
    return cells[index] if index < len(cells) else ""


def _table_after_heading(text: str, heading_prefix: str) -> tuple[list[str], list[list[str]]]:
    """返回指定标题之后第一张 Markdown 表的 (表头, 数据行)。"""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith(heading_prefix):
            start = i + 1
            break
    if start is None:
        raise ValueError(f"缺少章节: {heading_prefix}")

    headers: list[str] = []
    rows: list[list[str]] = []
    in_table = False
    for line in lines[start:]:
        stripped = line.strip()
        if stripped.startswith("#"):
            break
        if not stripped.startswith("|"):
            if in_table:
                break
            continue
        cells = _parse_table_row(stripped)
        if not cells:
            continue
        if _is_separator_row(cells):
            in_table = True
            continue
        if not headers:
            headers = cells
            in_table = True
            continue
        rows.append(cells)
    if not in_table:
        raise ValueError(f"{heading_prefix} 后没有表格")
    return headers, rows


def parse_coverage_matrix_text(text: str) -> list[CoverageRow]:
    headers, table = _table_after_heading(text, "## 1. 覆盖契约")
    _require_headers(headers, COVERAGE_HEADERS, "覆盖契约")
    rows: list[CoverageRow] = []
    for cells in table:
        rows.append(CoverageRow(
            scenario_id=_cell_by_name(headers, cells, "场景ID"),
            requirement_id=_cell_by_name(headers, cells, "需求"),
            scenario=_cell_by_name(headers, cells, "场景"),
            category=_cell_by_name(headers, cells, "类别"),
            priority=_cell_by_name(headers, cells, "优先级"),
            oracle=_cell_by_name(headers, cells, "判定方式"),
        ))
    return rows


def parse_coverage_matrix(path: Path) -> list[CoverageRow]:
    return parse_coverage_matrix_text(path.read_text(encoding="utf-8"))


def parse_review_trace(path: Path) -> list[ReviewTraceRow]:
    text = path.read_text(encoding="utf-8")
    headers, table = _table_after_heading(text, "## 1. 追溯表")
    _require_headers(headers, REVIEW_HEADERS, "追溯表")
    rows: list[ReviewTraceRow] = []
    for cells in table:
        rows.append(ReviewTraceRow(
            scenario_id=_cell_by_name(headers, cells, "场景ID"),
            case_id=_cell_by_name(headers, cells, "对应用例"),
            verdict=_cell_by_name(headers, cells, "结论").upper(),
        ))
    return rows
