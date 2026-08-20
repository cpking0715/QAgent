"""生成前范围澄清：草稿、确认语、写入用户测试需求。"""

from __future__ import annotations

import re
from pathlib import Path

from qagent.ingest import is_test_requirements_file
from qagent.server.jobs import JobStore

QUERY_SYNONYMS = {
    "性能": ("性能", "耗时", "sla", "吞吐", "时延", "秒", "qps", "并发", "响应时间"),
    "perf": ("性能", "耗时", "sla", "吞吐", "时延", "秒", "qps", "并发", "响应时间"),
}


def line_matches_query(line: str, query: str) -> bool:
    blob = line.lower()
    needle = query.lower().strip()
    if not needle:
        return True
    if needle in blob:
        return True
    for key, aliases in QUERY_SYNONYMS.items():
        if key in needle or needle in aliases:
            return any(alias in blob for alias in aliases)
    return False


def artifact_has_perf(text: str) -> bool:
    blob = text.lower()
    return any(alias in blob for alias in QUERY_SYNONYMS["性能"])

SCOPE_DRAFT = """已收到文档。生成前请确认测试范围（改完或回复「可以 / 全量」即开始）：

**必测模块：** 按 PRD / 设计文档中的功能与 API
**不测：** 第三方内部实现、像素级 UI
**建议都测：** 功能、接口、边界、异常
**请说明是否要：** 安全、性能、兼容
**规模：** 常规

直接改范围也可以，例如：「不测性能，只要主流程和接口」。"""

_CONFIRM = re.compile(
    r"^(可以|好的|确认|按草稿|按草稿跑|全量|直接生成|开始生成)([。.!！\s]*)$",
)


def inputs_include_test_requirements(store: JobStore, job_id: str) -> bool:
    folder = store.input_dir(job_id)
    if not folder.is_dir():
        return False
    return any(
        p.is_file() and is_test_requirements_file(p)
        for p in folder.iterdir()
        if not p.name.startswith(".")
    )


def is_scope_confirm(text: str) -> bool:
    stripped = text.strip()
    if _CONFIRM.match(stripped):
        return True
    return any(token in stripped for token in ("全量", "直接生成", "按 PRD 全覆盖"))


def write_user_scope(store: JobStore, job_id: str, user_text: str) -> Path:
    path = store.input_dir(job_id) / "测试需求.md"
    if is_scope_confirm(user_text) and len(user_text.strip()) <= 20:
        body = (
            "# 测试需求\n\n"
            "## 1. 测试范围\n\n"
            "**必测模块：** 按 PRD / 设计文档功能与 API\n\n"
            "**不测 / 低优先级：** 第三方内部实现、像素级 UI\n\n"
            "## 2. 测试类型要求\n\n"
            "- 功能、接口、边界、异常：必须\n"
            "- 安全、性能、兼容：按需（用户未特别排除则按文档 SLA 覆盖）\n"
        )
        if "全量" in user_text:
            body = (
                "# 测试需求\n\n"
                "## 1. 测试范围\n\n"
                "**必测模块：** 按 PRD 全覆盖\n\n"
                "**不测：** 无（用户要求全量）\n"
            )
    else:
        body = f"# 测试需求\n\n## 1. 测试范围\n\n{user_text.strip()}\n"
    path.write_text(body, encoding="utf-8")
    return path
