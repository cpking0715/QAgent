"""知识源单一致性测试：rules.yaml/schema 渲染进 prompt 与 SKILL.md，双份模板不漂移。"""

from __future__ import annotations

from pathlib import Path

from qagent.agent.prompts import build_testcases_prompt
from qagent.config import QAgentConfig, REPO_ROOT
from qagent.rules import load_rules
from qagent.server.chat import system_prompt
from qagent import skills_gen


def _config(tmp_path: Path, **overrides) -> QAgentConfig:
    base = dict(
        workspace=tmp_path,
        input_dir=tmp_path / "input",
        output_dir=tmp_path / "output",
    )
    base.update(overrides)
    return QAgentConfig(**base)


def test_case_count_rule_renders_from_rules_yaml(tmp_path):
    """用例数量规则来自 rules.yaml（覆盖优先 + 参考区间），不再手写两版。"""
    rules = load_rules()
    _, user = build_testcases_prompt("需求", "方案", "风险", "矩阵", _config(tmp_path))
    assert f"{rules.case_complex[0]}~{rules.case_complex[1]}" in user
    assert f"{rules.case_simple[0]}~{rules.case_simple[1]}" in user
    assert "数量以覆盖为准" in user


def test_templates_and_skill_copies_in_sync():
    """templates/ 与 skills/qa-orchestrator/templates/ 必须逐字节一致（防人工漂移）。"""
    root = REPO_ROOT / "templates"
    skill = REPO_ROOT / "skills" / "qa-orchestrator" / "templates"
    assert root.is_dir() and skill.is_dir()
    for path in sorted(root.iterdir()):
        if not path.is_file():
            continue
        counterpart = skill / path.name
        assert counterpart.is_file(), f"skill 模板缺少 {path.name}"
        assert counterpart.read_bytes() == path.read_bytes(), f"模板漂移: {path.name}"


def test_skill_md_generated_blocks_in_sync():
    """SKILL.md 生成块必须与 templates/ 同步（等价 python -m qagent.skills_gen --check）。"""
    changed = skills_gen.update_all(check=True)
    assert changed == [], f"生成块过期: {changed}"


def test_chat_system_renders_enum_and_limit_from_source(tmp_path):
    system = system_prompt(_config(tmp_path))
    rules = load_rules()
    assert f"不要一次输出超过 {rules.chat_max_cases_per_action} 条用例" in system
    for value in ("功能", "边界", "异常", "安全", "组合"):
        assert value in system


PLAN_WITH_BLOCK = """# 测试方案

## 2. 需求条目清单

```requirements
R1: 描述一
R2: 描述二
R3: 描述三
```

## 6. 测试设计技术选择

（正文内容）
"""


def test_sliced_prompt_keeps_only_batch_requirements(tmp_path):
    """sliced 模式：requirements 块只保留本批 R，长文档按预算截断。"""
    config = _config(
        tmp_path,
        prompt_context_mode="sliced",
        prompt_treq_budget=100,
        prompt_plan_budget=10000,
        prompt_risk_budget=100,
    )
    treq = "需求正文" + "长" * 500
    risk = "风险" + "险" * 500
    _, user = build_testcases_prompt(
        treq, PLAN_WITH_BLOCK, risk, "| 矩阵 |", config, requirement_ids=["R2"],
    )
    assert "R2: 描述二" in user
    assert "描述三" not in user and "描述一" not in user
    assert "已按预算截断" in user
    assert "长" * 200 not in user  # treq 已截断


def test_full_prompt_mode_keeps_everything_by_default(tmp_path):
    """默认 full 模式与历史行为一致：全文携带、不做 R 过滤。"""
    config = _config(tmp_path)
    treq = "需求正文" + "长" * 500
    _, user = build_testcases_prompt(
        treq, PLAN_WITH_BLOCK, "风险", "| 矩阵 |", config, requirement_ids=["R2"],
    )
    assert "长" * 500 in user
    assert "R1: 描述一" in user and "R3: 描述三" in user
