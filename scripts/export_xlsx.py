# /// script
# requires-python = ">=3.9"
# dependencies = ["pyyaml", "openpyxl"]
# ///
"""导出 testcases.xlsx（兼容入口，委托 qagent 包）。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qagent.cli import main

if __name__ == "__main__":
    if len(sys.argv) == 1:
        sys.argv.extend(["export"])
    elif Path(sys.argv[1]).suffix == ".md" and sys.argv[1] != "export":
        sys.argv.insert(1, "export")
    sys.exit(main())
