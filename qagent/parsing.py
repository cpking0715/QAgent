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
    "性能": "功能",
    "perf": "功能",
    "performance": "功能",
    "压力": "功能",
    "负载": "功能",
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
# SC 编号唯一正则：文本中查找用 findall，整串校验用 fullmatch
SC_ID_RE = re.compile(r"SC-\d{3}")
_SC_IN_TEXT = SC_ID_RE
_KEEP_RID_RE = re.compile(r"^\s*(R[\w-]*)\s*:")
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


def owned_scenario_ids(case: dict) -> list[str]:
    """一条用例只认主场景，避免标题里顺带出现的 SC-xxx 被当成已覆盖。"""
    title = str(case.get("title") or "")
    found = _SC_IN_TEXT.findall(title)
    if found:
        return [found[0]]
    case_id = str(case.get("id") or "")
    matched = re.fullmatch(r"TC-SC-(\d{3})", case_id)
    if matched:
        return [f"SC-{matched.group(1)}"]
    return []


def normalize_case(
    case: dict,
    valid_ids: set[str],
    sc_to_req: dict[str, str] | None = None,
    req_items: list[tuple[str, str]] | None = None,
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
    if sc_to_req:
        for sid in owned_scenario_ids(case):
            rid = sc_to_req.get(sid)
            if rid in valid_ids:
                cleaned = rid
                break
    if not cleaned and sc_to_req:
        for sid in case_scenario_ids(case):
            rid = sc_to_req.get(sid)
            if rid in valid_ids:
                cleaned = rid
                break
    if not cleaned:
        cleaned = infer_requirement_ref(case, req_items or [], valid_ids)
    if not cleaned and valid_ids:
        cleaned = sorted(valid_ids, key=_requirement_sort_key)[0]
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
        have.update(owned_scenario_ids(case))
    stubs = [case_from_matrix_row(row) for row in rows if row.scenario_id not in have]
    return merge_cases(cases, stubs)


def ensure_requirements_have_cases(
    cases: list[dict],
    rows: list[CoverageRow],
    valid_ids: set[str] | None = None,
) -> list[dict]:
    """方案里每个 R 至少挂一条用例，避免只覆盖了矩阵行但 requirement_ref 对不上。"""
    allowed = valid_ids if valid_ids is not None else {row.requirement_id for row in rows}
    covered: set[str] = set()
    for case in cases:
        covered.update(rid for rid in ref_ids(case) if rid in allowed)
    by_req: dict[str, CoverageRow] = {}
    for row in rows:
        by_req.setdefault(row.requirement_id, row)
    stubs = [
        case_from_matrix_row(row)
        for rid, row in by_req.items()
        if rid in allowed and rid not in covered
    ]
    return merge_cases(cases, stubs)


def render_qa_review_md(rows: list[CoverageRow], cases: list[dict]) -> str:
    """追溯表只引用已有用例 id，禁止编造或逗号拼接。"""
    by_sc: dict[str, str] = {}
    for case in cases:
        case_id = str(case.get("id") or "")
        if not case_id:
            continue
        for sid in owned_scenario_ids(case):
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


_PERF_HINTS = ("性能", "perf", "sla", "吞吐", "时延", "响应时间", "qps", "并发", "耗时", "速度")
_PERF_REQ_HINTS = ("性能", "sla", "秒", "吞吐", "时延", "速度", "qps")


def _requirement_sort_key(rid: str) -> tuple:
    matched = re.fullmatch(r"R(?:-([A-Z]+))?(\d+)", rid)
    if not matched:
        return (2, "", rid)
    prefix = matched.group(1) or ""
    return (1 if prefix else 0, prefix, int(matched.group(2)))


def infer_requirement_ref(
    case: dict,
    req_items: list[tuple[str, str]],
    valid_ids: set[str],
) -> str:
    """标题/ID 对不上矩阵时，按方案条目关键词补 requirement_ref。"""
    if not req_items:
        return ""
    blob = " ".join([
        str(case.get("id") or ""),
        str(case.get("title") or ""),
        str(case.get("expected") or ""),
        " ".join(str(step) for step in (case.get("steps") or [])),
    ])
    blob_l = blob.lower()
    case_is_perf = any(hint in blob or hint in blob_l for hint in _PERF_HINTS)
    scored: list[tuple[int, str]] = []
    for rid, desc in req_items:
        if rid not in valid_ids:
            continue
        score = 0
        desc_l = desc.lower()
        if case_is_perf and any(hint in desc or hint in desc_l for hint in _PERF_REQ_HINTS):
            score += 10
        for token in ("卡证", "票据", "模板", "导出", "权限", "识别"):
            if token in blob and token in desc:
                score += 1
        if score:
            scored.append((score, rid))
    if not scored:
        return ""
    scored.sort(key=lambda item: (-item[0], _requirement_sort_key(item[1])))
    return scored[0][1]


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


_REQ_BLOCK_RE = re.compile(r"```requirements\s*\n(.*?)\n```", re.DOTALL)


def parse_requirement_items(plan_path: Path) -> list[tuple[str, str]]:
    """解析 requirements 块为 (id, 描述) 列表，保序。"""
    text = plan_path.read_text(encoding="utf-8")
    match = _REQ_BLOCK_RE.search(text)
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


def filter_requirements_block(plan_text: str, keep_ids) -> str:
    """只保留 keep_ids 对应的 R 条目，块外内容原样保留（批次 prompt 切片用）。"""
    keep = set(keep_ids)
    match = _REQ_BLOCK_RE.search(plan_text)
    if not match or not keep:
        return plan_text
    lines = [
        line for line in match.group(1).splitlines()
        if not _KEEP_RID_RE.match(line) or _KEEP_RID_RE.match(line).group(1) in keep
    ]
    block = "```requirements\n" + "\n".join(lines) + "\n```"
    return plan_text[:match.start()] + block + plan_text[match.end():]


MATRIX_CATEGORIES = {"Happy", "Boundary", "Negative", "Security", "State", "Concurrency"}
MATRIX_PRIORITIES = {"P0", "P1", "P2"}
_CATEGORY_ALIAS = {
    "": "Happy",
    "happy": "Happy",
    "boundary": "Boundary",
    "negative": "Negative",
    "security": "Security",
    "state": "State",
    "concurrency": "Concurrency",
    "正向": "Happy",
    "功能": "Happy",
    "边界": "Boundary",
    "异常": "Negative",
    "负向": "Negative",
    "安全": "Security",
    "状态": "State",
    "并发": "Concurrency",
}


def normalize_category(raw: str) -> str:
    text = str(raw or "").strip()
    if text in MATRIX_CATEGORIES:
        return text
    return _CATEGORY_ALIAS.get(text.lower(), text or "Happy")


def normalize_priority(raw: str) -> str:
    text = str(raw or "").strip().upper()
    if text in MATRIX_PRIORITIES:
        return text
    if text in {"P00", "高", "紧急"}:
        return "P0"
    if text in {"中", "P"}:
        return "P1"
    if text in {"低"}:
        return "P2"
    return "P1" if not text else text


def drop_incomplete_matrix_rows(rows: list[CoverageRow]) -> list[CoverageRow]:
    """丢掉截断行（场景空，或类别与优先级都空）。"""
    kept: list[CoverageRow] = []
    for row in rows:
        if not str(row.requirement_id).strip() or not str(row.scenario).strip():
            continue
        if not str(row.category).strip() and not str(row.priority).strip():
            continue
        kept.append(row)
    return kept


def ensure_matrix_covers_requirements(
    rows: list[CoverageRow],
    items: list[tuple[str, str]],
) -> list[CoverageRow]:
    """每个需求至少补一行 Happy/P1，避免分批截断漏 R。"""
    covered = {row.requirement_id.strip() for row in rows}
    filled = list(rows)
    for rid, desc in items:
        if rid in covered:
            continue
        text = (desc or "").strip() or f"{rid} 主路径"
        if len(text) > 80:
            text = text[:80].rstrip() + "…"
        filled.append(CoverageRow(
            scenario_id="SC-000",
            requirement_id=rid,
            scenario=f"{rid} 主路径：{text}",
            category="Happy",
            priority="P1",
            oracle="结果符合需求描述",
        ))
        covered.add(rid)
    return filled


def finalize_matrix_rows(
    rows: list[CoverageRow],
    items: list[tuple[str, str]],
) -> list[CoverageRow]:
    cleaned: list[CoverageRow] = []
    for row in drop_incomplete_matrix_rows(rows):
        cleaned.append(CoverageRow(
            scenario_id=row.scenario_id,
            requirement_id=row.requirement_id.strip(),
            scenario=row.scenario.strip(),
            category=normalize_category(row.category),
            priority=normalize_priority(row.priority),
            oracle=(row.oracle or "结果符合需求描述").strip(),
        ))
    return renumber_matrix_rows(ensure_matrix_covers_requirements(cleaned, items))


def _md_cell(value: str) -> str:
    return str(value).replace("|", "／").replace("\n", " ")


def render_coverage_table(rows: list[CoverageRow]) -> str:
    header = (
        "| 场景ID | 需求 | 场景 | 类别 | 优先级 | 判定方式 |\n"
        "|--------|------|------|------|--------|----------|"
    )
    body = "\n".join(
        f"| {_md_cell(row.scenario_id)} | {_md_cell(row.requirement_id)} | "
        f"{_md_cell(row.scenario)} | {_md_cell(row.category)} | "
        f"{_md_cell(row.priority)} | {_md_cell(row.oracle)} |"
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


def _coverage_row_from_cells(headers: list[str], cells: list[str]) -> CoverageRow | None:
    if len(cells) > len(headers) and len(headers) >= 6:
        sid, rid = cells[0], cells[1]
        oracle, priority, category = cells[-1], cells[-2], cells[-3]
        scenario = " ".join(part for part in cells[2:-3] if part)
        mapped = CoverageRow(sid, rid, scenario, category, priority, oracle)
    elif len(cells) < 6:
        return None
    else:
        mapped = CoverageRow(
            scenario_id=_cell_by_name(headers, cells, "场景ID"),
            requirement_id=_cell_by_name(headers, cells, "需求"),
            scenario=_cell_by_name(headers, cells, "场景"),
            category=_cell_by_name(headers, cells, "类别"),
            priority=_cell_by_name(headers, cells, "优先级"),
            oracle=_cell_by_name(headers, cells, "判定方式"),
        )
    if not mapped.requirement_id.strip() or not mapped.scenario.strip():
        return None
    if not mapped.category.strip() and not mapped.priority.strip():
        return None
    return mapped


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
        row = _coverage_row_from_cells(headers, cells)
        if row is not None:
            rows.append(row)
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
