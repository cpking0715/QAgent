"""从测试需求生成 Draw.io / OPML / XMind 思维导图。"""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path
from uuid import uuid4
from xml.sax.saxutils import escape

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_LIST = re.compile(r"^(\s*)([-*+]|\d+\.)\s+(.*)$")
_SKIP_SECTIONS = ("文档来源", "覆盖矩阵")


def write_requirements_drawio(req_path: Path, drawio_path: Path) -> Path:
    """根据 test-requirements.md 写出 Draw.io。"""
    text = req_path.read_text(encoding="utf-8")
    tree = _build_requirements_tree(text)
    drawio_path.parent.mkdir(parents=True, exist_ok=True)
    drawio_path.write_text(_render_drawio(tree), encoding="utf-8")
    return drawio_path


def _first_heading(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _h2_sections(text: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("## "):
            title = line[3:].strip()
            title = re.sub(r"^\d+\.\s*", "", title)
            body_lines: list[str] = []
            for nxt in lines[i + 1:]:
                if nxt.startswith("## "):
                    break
                body_lines.append(nxt)
            sections.append((title, "\n".join(body_lines).strip()))
    return sections


def _is_sep_row(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c.replace(" ", "") or "-") for c in cells)


def _table_nodes(body: str) -> list[dict]:
    header = None
    nodes: list[dict] = []
    for line in body.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip().replace("**", "") for c in line.strip().strip("|").split("|")]
        if _is_sep_row(cells):
            continue
        if header is None:
            header = cells
            continue
        label = cells[0] if cells else ""
        extra = [c for c in cells[1:] if c and c not in {"-", "无", "/"}]
        text = f"{label} {' · '.join(extra[:3])}".strip() if extra else label
        if text and text != "无":
            nodes.append({"text": text[:80], "children": []})
    return nodes


def _bullet_nodes(body: str) -> list[dict]:
    nodes: list[dict] = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith(("- ", "* ")):
            text = re.sub(r"\*+", "", stripped[2:]).strip()
            if text and text != "无":
                nodes.append({"text": text[:80], "children": []})
    return nodes


def _section_nodes(title: str, body: str) -> list[dict]:
    if not body or body.strip() == "无":
        return []
    tables = _table_nodes(body)
    if tables:
        return tables
    bullets = _bullet_nodes(body)
    if bullets:
        return bullets
    snippet = re.sub(r"\*+", "", " ".join(body.split()))
    if snippet:
        return [{"text": snippet[:80], "children": []}]
    return []


def _build_requirements_tree(text: str) -> dict:
    title = _first_heading(text) or "测试需求"
    children: list[dict] = []
    for heading, body in _h2_sections(text):
        if any(skip in heading for skip in _SKIP_SECTIONS):
            continue
        nodes = _section_nodes(heading, body)
        if nodes:
            children.append({"text": heading, "children": nodes})
    if not children:
        children = [{"text": "（暂无要点）", "children": []}]
    return {"text": title, "children": children}


def markdown_to_opml(text: str) -> str:
    """把标题层级或嵌套列表 Markdown 转成 OPML 2.0。"""
    return _render_opml(parse_markdown_outline(text))


def markdown_to_drawio(text: str) -> str:
    """把标题层级或嵌套列表 Markdown 转成 Draw.io mxfile。"""
    return _render_drawio(parse_markdown_outline(text))


def markdown_to_xmind(text: str) -> bytes:
    """把标题层级或嵌套列表 Markdown 转成 XMind(.xmind) 二进制内容。

    返回的 bytes 是一个 ZIP 包，内含 ``content.json``，可直接存盘为 ``.xmind``。
    """
    return _render_xmind(parse_markdown_outline(text))


def write_xmind(source: Path, out: Path) -> Path:
    """把 Markdown 大纲（通用标题/嵌套列表）写入 ``.xmind`` 文件。"""
    text = source.read_text(encoding="utf-8")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(markdown_to_xmind(text))
    return out


def write_requirements_xmind(req_path: Path, xmind_path: Path) -> Path:
    """根据 test-requirements.md 写出与 Draw.io 同构的 XMind(.xmind)。"""
    text = req_path.read_text(encoding="utf-8")
    tree = _build_requirements_tree(text)
    xmind_path.parent.mkdir(parents=True, exist_ok=True)
    xmind_path.write_bytes(_render_xmind(tree))
    return xmind_path


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


_DRAWIO_H_GAP = 56
_DRAWIO_V_GAP = 14
_DRAWIO_STYLES = (
    "rounded=1;whiteSpace=wrap;html=0;fillColor=#dae8fc;strokeColor=#6c8ebf;fontStyle=1;align=center;",
    "rounded=1;whiteSpace=wrap;html=0;fillColor=#fff2cc;strokeColor=#d6b656;align=center;",
    "rounded=1;whiteSpace=wrap;html=0;fillColor=#f5f5f5;strokeColor=#666666;align=center;",
)
_DRAWIO_EDGE = (
    "endArrow=none;rounded=0;html=0;strokeColor=#9aa0a6;"
    "edgeStyle=orthogonalEdgeStyle;"
    "exitX=1;exitY=0.5;exitDx=0;exitDy=0;"
    "entryX=0;entryY=0.5;entryDx=0;entryDy=0;"
)


def _drawio_size(text: str) -> tuple[int, int]:
    raw = str(text)
    width = min(400, max(120, 12 * min(len(raw), 28) + 24))
    height = 32 if len(raw) <= 28 else 48
    return width, height


def _drawio_subtree_height(node: dict) -> int:
    _, height = _drawio_size(node["text"])
    children = node.get("children") or []
    if not children:
        return height
    return max(
        height,
        sum(_drawio_subtree_height(child) for child in children)
        + _DRAWIO_V_GAP * (len(children) - 1),
    )


def _drawio_place(
    node: dict,
    x: float,
    y_top: float,
    parent_id: str | None,
    acc: list[tuple],
    next_id: list[int],
    depth: int,
) -> None:
    width, height = _drawio_size(node["text"])
    total = _drawio_subtree_height(node)
    nid = str(next_id[0])
    next_id[0] += 1
    acc.append((nid, parent_id, str(node["text"]), x, y_top + (total - height) / 2, width, height, depth))
    child_y = y_top
    for child in node.get("children") or []:
        _drawio_place(child, x + width + _DRAWIO_H_GAP, child_y, nid, acc, next_id, depth + 1)
        child_y += _drawio_subtree_height(child) + _DRAWIO_V_GAP


def _render_drawio(node: dict) -> str:
    placed: list[tuple] = []
    _drawio_place(node, 40, 40, None, placed, [2], 0)
    max_x = max((x + w for _, _, _, x, _, w, _, _ in placed), default=800)
    max_y = max((y + h for _, _, _, _, y, _, h, _ in placed), default=600)
    page_w = max(827, int(max_x + 80))
    page_h = max(1169, int(max_y + 80))
    cells = [
        '        <mxCell id="0"/>\n',
        '        <mxCell id="1" parent="0"/>\n',
    ]
    coords = {nid: (x, y, w, h) for nid, _, _, x, y, w, h, _ in placed}
    for nid, parent_id, text, x, y, w, h, depth in placed:
        value = escape(text, {'"': "&quot;"})
        style = _DRAWIO_STYLES[min(depth, len(_DRAWIO_STYLES) - 1)]
        cells.append(
            f'        <mxCell id="{nid}" value="{value}" style="{style}" '
            f'vertex="1" parent="1">\n'
            f'          <mxGeometry x="{x:.0f}" y="{y:.0f}" width="{w}" height="{h}" as="geometry"/>\n'
            f"        </mxCell>\n"
        )
        if parent_id is not None:
            eid = f"e{nid}"
            px, py, pw, ph = coords[parent_id]
            pcy = py + ph / 2
            ccy = y + h / 2
            mid_x = x - _DRAWIO_H_GAP / 2
            cells.append(
                f'        <mxCell id="{eid}" style="{_DRAWIO_EDGE}" '
                f'edge="1" parent="1" source="{parent_id}" target="{nid}">\n'
                f'          <mxGeometry relative="1" as="geometry">\n'
                f'            <Array as="points">\n'
                f'              <mxPoint x="{px + pw:.0f}" y="{pcy:.0f}"/>\n'
                f'              <mxPoint x="{mid_x:.0f}" y="{pcy:.0f}"/>\n'
                f'              <mxPoint x="{mid_x:.0f}" y="{ccy:.0f}"/>\n'
                f'              <mxPoint x="{x:.0f}" y="{ccy:.0f}"/>\n'
                f"            </Array>\n"
                f"          </mxGeometry>\n"
                f"        </mxCell>\n"
            )
    title = escape(str(node.get("text") or "测试需求"))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<mxfile host="QAgent" agent="QAgent">\n'
        f'  <diagram id="plan" name="{title}">\n'
        f'    <mxGraphModel dx="1200" dy="800" grid="1" gridSize="10" guides="1" '
        f'tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" '
        f'pageWidth="{page_w}" pageHeight="{page_h}" math="0" shadow="0">\n'
        "      <root>\n"
        f"{''.join(cells)}"
        "      </root>\n"
        "    </mxGraphModel>\n"
        "  </diagram>\n"
        "</mxfile>\n"
    )


def _xmind_topic(node: dict) -> dict:
    topic: dict = {"id": uuid4().hex, "title": str(node.get("text") or "未命名")}
    children = node.get("children") or []
    if children:
        topic["children"] = {"attached": [_xmind_topic(child) for child in children]}
    return topic


def _render_xmind(node: dict) -> bytes:
    title = str(node.get("text") or "导图")
    sheet_id = uuid4().hex
    content = [
        {
            "id": sheet_id,
            "class": "sheet",
            "title": title,
            "rootTopic": _xmind_topic(node),
        }
    ]
    # XMind 2020+ 打开时强校验 metadata.json（缺失即报
    # "MUST have a metadata.json file"），manifest 需登记全部条目
    metadata = {
        "creator": {"name": "QAgent", "version": "1.0"},
        "activeSheetId": sheet_id,
    }
    manifest = {
        "file-entries": {"content.json": {}, "metadata.json": {}},
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "content.json",
            json.dumps(content, ensure_ascii=False, indent=2),
        )
        zf.writestr(
            "metadata.json",
            json.dumps(metadata, ensure_ascii=False, indent=2),
        )
        zf.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )
    return buffer.getvalue()
