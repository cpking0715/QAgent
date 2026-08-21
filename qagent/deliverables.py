"""流水线产物说明：文件名、作用、内容。"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

GUIDE: tuple[dict[str, str], ...] = (
    {
        "key": "test_requirements",
        "title": "测试需求",
        "role": "测什么、不测什么",
        "content": "从 PRD / 设计抽出测试范围、功能点、规则与优先级，作为后续方案和用例的覆盖依据。",
    },
    {
        "key": "test_requirements_xmind",
        "title": "需求导图（XMind）",
        "role": "测试需求的可视化",
        "content": "与 test-requirements.md 同一棵树，额外导出为 XMind(.xmind)，用 XMind 2020+ 打开。",
    },
    {
        "key": "test_plan",
        "title": "测试方案",
        "role": "怎么测",
        "content": "需求条目（R1…）、测试目标、范围、测试类型与层级、环境与策略。这是方案正文。",
    },
    {
        "key": "risk",
        "title": "风险分析",
        "role": "先测哪里、重点防什么",
        "content": "按影响 × 可能性打分，列出高风险项和建议关注的测试。",
    },
    {
        "key": "coverage_matrix",
        "title": "覆盖矩阵",
        "role": "覆盖契约",
        "content": "每个场景（SC-xxx）对应哪条需求、类别、优先级、如何判定。写用例前先定这张表。",
    },
    {
        "key": "testcases",
        "title": "测试用例",
        "role": "可执行步骤",
        "content": "逐步操作、预期结果、优先级，并关联需求 / 场景，供人阅读。",
    },
    {
        "key": "xlsx",
        "title": "用例表格",
        "role": "同一批用例的 Excel",
        "content": "与 testcases.md 同步，便于导入用例平台或给测试同学填执行结果。",
    },
    {
        "key": "qa_review",
        "title": "QA Review",
        "role": "查漏与追溯",
        "content": "场景 SC 与用例 TC 的覆盖结论（COVERED / GAP / DUPLICATE / WEAK），以及缺口和用例味道。",
    },
)


def list_deliverables(artifacts: Mapping[str, str | Path]) -> list[dict[str, str]]:
    """按流水线顺序列出已存在的产物及其说明。"""
    found: dict[str, str | Path] = dict(artifacts)
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in GUIDE:
        raw = found.get(item["key"])
        if raw is None:
            continue
        path = Path(str(raw))
        rows.append({
            "key": item["key"],
            "step": str(len(rows) + 1),
            "title": item["title"],
            "role": item["role"],
            "content": item["content"],
            "file": path.name,
            "path": str(raw),
        })
        seen.add(item["key"])
    for key, raw in found.items():
        if key in seen:
            continue
        path = Path(str(raw))
        rows.append({
            "key": key,
            "step": str(len(rows) + 1),
            "title": path.name,
            "role": "其他产物",
            "content": "",
            "file": path.name,
            "path": str(raw),
        })
    return rows


def format_deliverables(artifacts: Mapping[str, str | Path], case_count: int | None = None) -> str:
    rows = list_deliverables(artifacts)
    if not rows:
        return "尚未生成产物。"
    lines = ["已生成测试产物："]
    if case_count is not None:
        lines[0] = f"已生成测试产物（{case_count} 条用例）："
    for row in rows:
        lines.append("")
        lines.append(f"{row['step']}. {row['title']}（{row['file']}）")
        lines.append(f"   作用：{row['role']}")
        if row["content"]:
            lines.append(f"   内容：{row['content']}")
    return "\n".join(lines)
