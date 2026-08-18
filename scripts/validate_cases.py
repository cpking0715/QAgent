# /// script
# requires-python = ">=3.9"
# dependencies = ["pyyaml"]
# ///
"""校验 testcases.md（兼容入口，委托 qagent 包）。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qagent.cli import main

if __name__ == "__main__":
    if len(sys.argv) == 1:
        sys.argv.extend(["validate"])
    elif Path(sys.argv[1]).suffix == ".md" and sys.argv[1] != "validate":
        sys.argv.insert(1, "validate")
    sys.exit(main())
