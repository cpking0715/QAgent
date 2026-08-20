"""从 templates/（rules.yaml + testcase.schema.yaml）生成 SKILL.md 中的规则段落。

标记块外的内容一律保留，块内由生成器重写：

    <!-- qagent:gen:case_count -->
    …（生成内容，勿手改）…
    <!-- /qagent:gen:case_count -->

用法：
    python -m qagent.skills_gen          # 就地更新 SKILL.md 生成块
    python -m qagent.skills_gen --check  # 只校验同步（CI 用，不一致退出码 1）
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from qagent.config import REPO_ROOT
from qagent.rules import load_rules
from qagent.schema import load_schema

SKILLS_DIR = REPO_ROOT / "skills"
TARGETS = [
    SKILLS_DIR / "qa-testcase-generator" / "SKILL.md",
    SKILLS_DIR / "qa-test-design" / "SKILL.md",
]

_BLOCK_RE = re.compile(
    r"<!-- qagent:gen:(?P<key>[\w-]+) -->\n(?P<body>.*?)\n<!-- /qagent:gen:(?P=key) -->",
    re.DOTALL,
)


def _render(key: str) -> str:
    rules = load_rules()
    schema = load_schema(REPO_ROOT / "templates" / "testcase.schema.yaml")
    if key == "case_count":
        return f"6. **总量控制**：{rules.case_count_rule()}"
    if key == "risk_zones":
        crit = schema.risk_zones.get("CRITICAL", {}).get("min_score", 15)
        high = schema.risk_zones.get("HIGH", {}).get("min_score", 10)
        return f"3. **分区**：CRITICAL ≥{crit}，HIGH ≥{high}，MEDIUM 5-9，LOW 1-4。"
    raise ValueError(f"未知生成块: {key}")


def update_file(path: Path, check: bool = False) -> bool:
    """重写 path 中的标记块；返回是否有变更（check=True 时只比较不写盘）。"""
    text = path.read_text(encoding="utf-8")

    def _repl(match: re.Match) -> str:
        key = match.group("key")
        return f"<!-- qagent:gen:{key} -->\n{_render(key)}\n<!-- /qagent:gen:{key} -->"

    updated = _BLOCK_RE.sub(_repl, text)
    if updated != text and not check:
        path.write_text(updated, encoding="utf-8")
    return updated != text


def update_all(check: bool = False) -> list[Path]:
    changed: list[Path] = []
    for path in TARGETS:
        if path.is_file() and update_file(path, check):
            changed.append(path)
    return changed


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    check = "--check" in args
    changed = update_all(check=check)
    if check:
        if changed:
            print("以下 SKILL.md 生成块与 templates/ 不同步：")
            for path in changed:
                print(f"  {path}")
            print("运行 python -m qagent.skills_gen 更新")
            return 1
        print("SKILL.md 生成块与 templates/ 同步")
        return 0
    for path in changed:
        print(f"已更新 {path}")
    if not changed:
        print("无变更")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
