# /// script
# requires-python = ">=3.9"
# dependencies = ["pyyaml"]
# ///
"""testcases.md / test-plan.md 解析公共逻辑。

契约定义见 templates/testcase.yaml：
- testcases.md 中每条用例是一个 ```yaml 围栏块
- test-plan.md 的 requirements 代码块格式为 `RID: 描述`
"""

import re
from pathlib import Path

import yaml

REQUIRED_FIELDS = ["id", "title", "priority", "type", "steps", "expected",
                   "design_method", "requirement_ref"]
ENUMS = {
    "priority": {"P0", "P1", "P2"},
    "type": {"功能", "边界", "异常", "安全", "组合"},
    "design_method": {"等价类", "边界值", "状态转换", "判定表", "pairwise",
                      "错误推测", "场景法"},
}
ID_PATTERN = re.compile(r"^TC-[A-Z0-9]{2,8}-\d{3}$")


def parse_cases(cases_path: Path) -> list:
    """解析 testcases.md，返回所有 yaml 用例块（dict 列表）。"""
    text = cases_path.read_text(encoding="utf-8")
    blocks = re.findall(r"```yaml\s*\n(.*?)\n```", text, re.DOTALL)
    cases = []
    for i, block in enumerate(blocks):
        try:
            data = yaml.safe_load(block)
        except yaml.YAMLError as exc:
            raise ValueError(f"第 {i + 1} 个 yaml 块解析失败: {exc}")
        if not isinstance(data, dict):
            raise ValueError(f"第 {i + 1} 个 yaml 块不是键值结构")
        cases.append(data)
    return cases


def parse_requirement_ids(plan_path: Path) -> set:
    """从 test-plan.md 的 requirements 代码块提取需求 ID 集合。"""
    text = plan_path.read_text(encoding="utf-8")
    match = re.search(r"```requirements\s*\n(.*?)\n```", text, re.DOTALL)
    if not match:
        raise ValueError("test-plan.md 缺少 ```requirements 代码块")
    ids = set()
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rid = line.split(":", 1)[0].strip()
        if rid:
            ids.add(rid)
    if not ids:
        raise ValueError("requirements 代码块为空")
    return ids


def ref_ids(case: dict) -> list:
    """requirement_ref 支持 'R1' 或 'R1,R2' 两种写法。"""
    raw = case.get("requirement_ref", "")
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def validate(cases: list, requirement_ids: set) -> tuple:
    """返回 (errors, warnings)。errors 非空即校验失败。"""
    errors, warnings = [], []
    seen_ids = set()

    if not cases:
        errors.append("testcases.md 中没有任何 yaml 用例块")
        return errors, warnings

    covered = set()
    for idx, case in enumerate(cases, start=1):
        label = case.get("id") or f"第{idx}条"

        for field in REQUIRED_FIELDS:
            value = case.get(field)
            if value in (None, "", []):
                errors.append(f"{label}: 缺少必填字段 {field}")

        for field, allowed in ENUMS.items():
            value = case.get(field)
            if value is not None and str(value) not in allowed:
                errors.append(
                    f"{label}: {field}={value!r} 不在枚举 {sorted(allowed)} 内")

        case_id = case.get("id")
        if case_id:
            if not ID_PATTERN.match(str(case_id)):
                errors.append(
                    f"{label}: id 不符合 TC-<模块缩写>-<3位序号> 格式")
            if case_id in seen_ids:
                errors.append(f"{label}: id 重复")
            seen_ids.add(case_id)

        for field in ("steps", "preconditions"):
            value = case.get(field)
            if value is not None and not isinstance(value, list):
                errors.append(f"{label}: {field} 必须是列表")

        refs = ref_ids(case)
        if refs:
            unknown = [r for r in refs if r not in requirement_ids]
            if unknown:
                errors.append(
                    f"{label}: requirement_ref 引用了不存在的需求 {unknown}，"
                    f"可用需求 ID: {sorted(requirement_ids)}")
            covered.update(r for r in refs if r in requirement_ids)

    uncovered = requirement_ids - covered
    if uncovered:
        warnings.append(f"以下需求条目没有用例覆盖: {sorted(uncovered)}")

    return errors, warnings
