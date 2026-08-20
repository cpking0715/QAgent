#!/usr/bin/env python3
"""把 QAgent 技能拷到 Cursor / 其它 IDE，并尝试安装 CLI。macOS / Windows / Linux 通用。"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS_SRC = ROOT / "skills"
SKILLS = ("qa-orchestrator", "qa-test-design", "qa-testcase-generator")


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    skip_pip = False
    paths = []
    for item in args:
        if item == "--skip-pip":
            skip_pip = True
        else:
            paths.append(item)
    raw = paths[0] if paths else ".cursor/skills"
    target = Path(raw)
    if not target.is_absolute():
        target = Path.cwd() / target
    target.mkdir(parents=True, exist_ok=True)

    for skill in SKILLS:
        src = SKILLS_SRC / skill
        if not src.is_dir():
            print(f"ERROR: 缺少技能目录 {src}", file=sys.stderr)
            return 1
        dest = target / skill
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)
        print(f"已安装: {dest}")

    if skip_pip:
        print("已跳过 pip 安装")
    else:
        pip = [sys.executable, "-m", "pip", "install", "-e", str(ROOT)]
        try:
            subprocess.run(pip, check=True)
            print("已安装 qagent CLI: qagent --help")
        except (OSError, subprocess.CalledProcessError) as exc:
            print(f"警告: 未能自动安装 CLI（{exc}）")
            print(f"请手动执行: {sys.executable} -m pip install -e {ROOT}")

    print("")
    print("验证命令:")
    print("  qagent --help")
    print("  qagent pipeline status")
    return 0


if __name__ == "__main__":
    sys.exit(main())
