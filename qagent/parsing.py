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


def parse_cases(cases_path: Path) -> list[dict]:
    """解析 testcases.md，返回所有 yaml 用例块。"""
    text = cases_path.read_text(encoding="utf-8")
    blocks = re.findall(r"```yaml\s*\n(.*?)\n```", text, re.DOTALL)
    cases: list[dict] = []
    for i, block in enumerate(blocks):
        try:
            data = yaml.safe_load(block)
        except yaml.YAMLError as exc:
            raise ValueError(f"第 {i + 1} 个 yaml 块解析失败: {exc}") from exc
        if isinstance(data, dict):
            cases.append(data)
        elif isinstance(data, list):
            for j, item in enumerate(data):
                if not isinstance(item, dict):
                    raise ValueError(
                        f"第 {i + 1} 个 yaml 块第 {j + 1} 项不是键值结构",
                    )
                cases.append(item)
        else:
            raise ValueError(f"第 {i + 1} 个 yaml 块不是键值结构")
    return cases


def parse_requirement_ids(plan_path: Path) -> set[str]:
    """从 test-plan.md 的 requirements 代码块提取需求 ID 集合。"""
    text = plan_path.read_text(encoding="utf-8")
    match = re.search(r"```requirements\s*\n(.*?)\n```", text, re.DOTALL)
    if not match:
        raise ValueError("test-plan.md 缺少 ```requirements 代码块")
    ids: set[str] = set()
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rid = line.split(":", 1)[0].strip()
        if rid:
            ids.add(rid)
    if not ids:
        raise ValueError("requirements 代码块为空")
    return ids


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


def parse_coverage_matrix(path: Path) -> list[CoverageRow]:
    text = path.read_text(encoding="utf-8")
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
