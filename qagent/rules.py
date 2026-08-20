"""从 templates/rules.yaml 加载可调数值规则（唯一事实来源）。

prompt 渲染、对话修订约束与 SKILL.md 生成段落都消费这里的 QARules，
摸索期调整标准只改 rules.yaml 一个文件。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from qagent.config import REPO_ROOT


@dataclass(frozen=True)
class QARules:
    coverage_phrase: str
    case_simple: tuple[int, int]
    case_complex: tuple[int, int]
    case_complex_when: str
    case_trim_order: str
    checklist_rows_simple: int
    checklist_rows_complex: int
    chat_max_cases_per_action: int

    def case_count_rule(self) -> str:
        """渲染进 prompt / SKILL.md 的数量规则：覆盖优先，区间仅作参考。"""
        return (
            f"数量以覆盖为准（{self.coverage_phrase}）；"
            f"{self.case_complex_when}约 {self.case_complex[0]}~{self.case_complex[1]} 条，"
            f"简单功能约 {self.case_simple[0]}~{self.case_simple[1]} 条——"
            f"区间仅作自查参考，明显偏离时检查是否漏测或冗余，禁止为凑数增删用例；"
            f"{self.case_trim_order}"
        )

    def checklist_rule(self) -> str:
        return (
            f"第 3~5 节表格行数参考：简单功能 ≥{self.checklist_rows_simple} 行，"
            f"复杂系统（OCR/多 API）≥{self.checklist_rows_complex} 行"
        )


def default_rules_path() -> Path:
    return REPO_ROOT / "templates" / "rules.yaml"


@lru_cache(maxsize=8)
def load_rules(path: Path | None = None) -> QARules:
    path = path or default_rules_path()
    raw: dict = {}
    if path.is_file():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        raw = loaded if isinstance(loaded, dict) else {}

    coverage = raw.get("coverage") or {}
    cc = raw.get("case_count") or {}
    tr = raw.get("test_requirements") or {}
    chat = raw.get("chat") or {}

    def _range(value: object, fallback: tuple[int, int]) -> tuple[int, int]:
        if isinstance(value, (list, tuple)) and len(value) == 2:
            return int(value[0]), int(value[1])
        return fallback

    return QARules(
        coverage_phrase=str(coverage.get("phrase") or "矩阵每行 ≥1 条、每个 R ≥1 条正向"),
        case_simple=_range(cc.get("simple"), (15, 40)),
        case_complex=_range(cc.get("complex"), (30, 80)),
        case_complex_when=str(cc.get("complex_when") or "复杂系统（需求条目 ≥15 或多 PDF）"),
        case_trim_order=str(cc.get("trim_order") or "需要精简时优先砍 LOW 风险对应的 P2 用例"),
        checklist_rows_simple=int(tr.get("checklist_rows_simple") or 8),
        checklist_rows_complex=int(tr.get("checklist_rows_complex") or 25),
        chat_max_cases_per_action=int(chat.get("max_cases_per_action") or 8),
    )
