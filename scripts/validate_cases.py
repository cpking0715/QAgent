# /// script
# requires-python = ">=3.9"
# dependencies = ["pyyaml"]
# ///
"""校验 output/testcases.md 是否符合用例 Schema 契约。

用法:
    python scripts/validate_cases.py output/testcases.md --plan output/test-plan.md

退出码: 0 = 通过(OK)，1 = 存在错误。覆盖缺口以警告形式输出，不阻断。
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from qa_common import parse_cases, parse_requirement_ids, validate


def main() -> int:
    parser = argparse.ArgumentParser(description="校验测试用例文件")
    parser.add_argument("cases", type=Path, help="testcases.md 路径")
    parser.add_argument("--plan", type=Path, required=True,
                        help="test-plan.md 路径（用于解析需求条目清单）")
    args = parser.parse_args()

    try:
        cases = parse_cases(args.cases)
        requirement_ids = parse_requirement_ids(args.plan)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1

    errors, warnings = validate(cases, requirement_ids)

    for warning in warnings:
        print(f"WARNING: {warning}")
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        print(f"FAILED: 共 {len(errors)} 个错误")
        return 1

    print(f"OK: {len(cases)} 条用例全部通过校验，"
          f"需求条目 {len(requirement_ids)} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
