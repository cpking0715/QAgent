"""技能包内脚本引导：优先 qagent CLI，否则回退到工作区 qagent 包。"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def skill_root() -> Path:
    return Path(__file__).resolve().parent.parent


def workspace_root() -> Path:
    return Path.cwd()


def run_qagent(argv: list[str]) -> int:
    if shutil.which("qagent"):
        result = subprocess.run(["qagent", *argv], check=False)
        return result.returncode

    for root in (workspace_root(), skill_root().parent.parent):
        if (root / "qagent" / "cli.py").is_file():
            sys.path.insert(0, str(root))
            from qagent.cli import main
            return main(argv)

    print("ERROR: 未找到 qagent。请运行: pip install -e <QAgent 仓库根目录>")
    return 1


if __name__ == "__main__":
    sys.exit(run_qagent(sys.argv[1:]))
