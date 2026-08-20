"""从测试方案等产物生成可导入飞书的思维导图（Markdown / FreeMind / OPML）。"""

from __future__ import annotations

import re
from pathlib import Path
from xml.sax.saxutils import escape

from qagent.parsing import (
    CoverageRow,
    RiskItem,
    parse_coverage_matrix,
    parse_requirement_items,
    parse_risks,
)

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_LIST = re.compile(r"^(\s*)([-*+]|\d+\.)\s+(.*)$")


def write_test_plan_mindmaps(
    plan_path: Path,
    md_path: Path,
    mm_path: Path,
    matrix_path: Path | None = None,
    risk_path: Path | None = None,
    opml_path: Path | None = None,
) -> tuple[Path, Path, Path]:
    """写出 Markdown 大纲、FreeMind XML 与 OPML（飞书可直接导入）。"""
    plan_text = plan_path.read_text(encoding="utf-8")
    title = _first_heading(plan_text) or "测试方案"
    reqs = parse_requirement_items(plan_path)
    matrix_rows: list[CoverageRow] = []
    if matrix_path and matrix_path.is_file():
        try:
            matrix_rows = parse_coverage_matrix(matrix_path)
        except ValueError:
            matrix_rows = []
    risks: list[RiskItem] = []
    if risk_path and risk_path.is_file():
        try:
            risks = parse_risks(risk_path)
        except ValueError:
            risks = []

    scope = _section_body(plan_text, "## 4. 测试范围")
    tree = _build_tree(title, scope, reqs, matrix_rows, risks)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(_render_markdown(tree), encoding="utf-8")
    mm_path.write_text(_render_freemind(tree), encoding="utf-8")
    opml_file = opml_path or mm_path.with_suffix(".opml")
    opml_file.write_text(_render_opml(tree), encoding="utf-8")
    return md_path, mm_path, opml_file


def _first_heading(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _section_body(text: str, heading: str) -> str:
    lines = text.splitlines()
    start = None
    level = 0
    for i, line in enumerate(lines):
        if line.startswith(heading):
            start = i + 1
            level = len(line) - len(line.lstrip("#"))
            break
    if start is None:
        return ""
    collected: list[str] = []
    for line in lines[start:]:
        if line.startswith("#"):
            n = len(line) - len(line.lstrip("#"))
            if n <= level:
                break
        collected.append(line)
    return "\n".join(collected).strip()


def _build_tree(
    title: str,
    scope: str,
    reqs: list[tuple[str, str]],
    matrix_rows: list[CoverageRow],
    risks: list[RiskItem],
) -> dict:
    by_req: dict[str, list[CoverageRow]] = {}
    for row in matrix_rows:
        by_req.setdefault(row.requirement_id, []).append(row)

    req_children: list[dict] = []
    for rid, desc in reqs:
        children: list[dict] = []
        grouped: dict[str, list[CoverageRow]] = {}
        for row in by_req.get(rid, []):
            grouped.setdefault(row.category or "未分类", []).append(row)
        for category, rows in grouped.items():
            children.append({
                "text": category,
                "children": [
                    {"text": f"{row.scenario_id} {row.scenario}".strip(), "children": []}
                    for row in rows
                ],
            })
        req_children.append({
            "text": f"{rid} {desc}".strip(),
            "children": children,
        })

    scope_children = [
        {"text": line.lstrip("- ").strip(), "children": []}
        for line in scope.splitlines()
        if line.strip().startswith("-")
    ]
    if not scope_children and scope:
        snippet = " ".join(scope.split())
        scope_children = [{"text": snippet[:80], "children": []}]

    risk_sorted = sorted(risks, key=lambda r: r.score, reverse=True)[:8]
    risk_children = [
        {
            "text": f"{item.risk_id} {item.description}（{item.zone} {item.score}）",
            "children": [],
        }
        for item in risk_sorted
    ]

    children = [
        {"text": "范围", "children": scope_children},
        {"text": "需求", "children": req_children},
    ]
    if risk_children:
        children.append({"text": "风险", "children": risk_children})
    return {"text": title, "children": children}


def _render_markdown(node: dict, level: int = 1) -> str:
    if level == 1:
        header = (
            f"# {node['text']}\n\n"
            "> 导入飞书：优先用同目录 `test-plan.opml`（思维导图 → 导入 → OPML）；"
            "也可导入本 Markdown，或 `test-plan.mm`（XMind / MindManager）。\n\n"
        )
        body = "".join(_render_markdown(child, level + 1) for child in node["children"])
        return header + body
    hashes = "#" * min(level, 6)
    lines = [f"{hashes} {node['text']}\n"]
    if level >= 6:
        for child in node["children"]:
            lines.append(f"- {child['text']}\n")
            for grandchild in child.get("children") or []:
                lines.append(f"  - {grandchild['text']}\n")
        lines.append("\n")
        return "".join(lines)
    for child in node["children"]:
        lines.append(_render_markdown(child, level + 1))
    if not node["children"]:
        lines.append("\n")
    return "".join(lines)


def _render_freemind(node: dict) -> str:
    inner = _mm_node(node)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<map version="1.0.1">\n'
        f"{inner}"
        "</map>\n"
    )


def _mm_node(node: dict, indent: int = 1) -> str:
    pad = "  " * indent
    text = escape(str(node["text"]), {'"': "&quot;"})
    children = node.get("children") or []
    if not children:
        return f'{pad}<node TEXT="{text}"/>\n'
    parts = [f'{pad}<node TEXT="{text}">\n']
    for child in children:
        parts.append(_mm_node(child, indent + 1))
    parts.append(f"{pad}</node>\n")
    return "".join(parts)


def markdown_to_opml(text: str) -> str:
    """把标题层级或嵌套列表 Markdown 转成 OPML 2.0。"""
    return _render_opml(parse_markdown_outline(text))


def parse_markdown_outline(text: str) -> dict:
    """解析 ATX 标题与 `-` / `*` / `1.` 嵌套列表为导图树。"""
    root = {"text": "导图", "children": []}
    stack: list[tuple[int, dict]] = [(0, root)]
    heading_level = 0
    list_indents: list[int] = []
    in_fence = False

    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not stripped or stripped.startswith(">"):
            continue

        heading = _HEADING.match(raw)
        if heading:
            level = len(heading.group(1))
            node = {"text": _clean_md_text(heading.group(2)), "children": []}
            while stack and stack[-1][0] >= level:
                stack.pop()
            if not stack:
                stack = [(0, root)]
            stack[-1][1]["children"].append(node)
            stack.append((level, node))
            heading_level = level
            list_indents = []
            continue

        listed = _LIST.match(raw)
        if listed:
            indent = len(listed.group(1).replace("\t", "  "))
            while list_indents and list_indents[-1] > indent:
                list_indents.pop()
            if not list_indents or indent > list_indents[-1]:
                list_indents.append(indent)
            level = heading_level + len(list_indents)
            node = {"text": _clean_md_text(listed.group(3)), "children": []}
            while stack and stack[-1][0] >= level:
                stack.pop()
            if not stack:
                stack = [(0, root)]
            stack[-1][1]["children"].append(node)
            stack.append((level, node))

    children = root["children"]
    if len(children) == 1:
        return children[0]
    return root


def _clean_md_text(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"\s+#+\s*$", "", cleaned)
    cleaned = re.sub(r"\*\*(.+?)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)
    return cleaned.strip() or "未命名"


def _render_opml(node: dict) -> str:
    title = escape(str(node.get("text") or "导图"))
    body = _opml_outline(node, indent=2)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<opml version="2.0">\n'
        "  <head>\n"
        f"    <title>{title}</title>\n"
        "  </head>\n"
        "  <body>\n"
        f"{body}"
        "  </body>\n"
        "</opml>\n"
    )


def _opml_outline(node: dict, indent: int = 2) -> str:
    pad = "  " * indent
    text = escape(str(node["text"]), {'"': "&quot;"})
    children = node.get("children") or []
    if not children:
        return f'{pad}<outline text="{text}"/>\n'
    parts = [f'{pad}<outline text="{text}">\n']
    for child in children:
        parts.append(_opml_outline(child, indent + 1))
    parts.append(f"{pad}</outline>\n")
    return "".join(parts)
