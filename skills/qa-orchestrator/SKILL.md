---
name: qa-orchestrator
description: QA 测试方案与用例生成流水线的总入口。从需求文档出发，按固定顺序生成测试方案、风险分析、结构化测试用例并导出 xlsx。当用户使用 /qa 命令、要求"生成测试方案/测试用例"、或提供 PRD/需求文档要求做测试设计时使用。
---

# QA Orchestrator（测试生成流水线）

固定流水线入口。**严格按顺序执行以下步骤，不跳步、不改序、不做条件分支路由。**

## 触发方式

- 命令式：`/qa generate <需求文件路径>`（默认路径 `input/requirement-example.md`）
- 自然语言："帮我根据这个需求生成测试方案和用例"等类似表述

## 流水线步骤

```
Task Progress:
- [ ] Step 1: 解析需求，提取需求条目清单 R1..Rn
- [ ] Step 2: 生成 output/test-plan.md
- [ ] Step 3: 生成 output/risk.md
- [ ] Step 4: 生成 output/testcases.md
- [ ] Step 5: 运行 validate_cases.py 校验，失败则修正重试
- [ ] Step 6: 运行 export_xlsx.py 导出 output/testcases.xlsx
```

### Step 1：解析需求

读取需求文件，提炼为编号的需求条目清单（R1、R2、...）。每条是一个独立可验证的业务规则。
含糊或缺失的规则（如错误提示文案、锁定解除时间）标注为"需求假设"，写入 test-plan.md 并在回复中提示用户确认。

### Step 2：生成测试方案

按 qa-test-design 技能的方法与 `templates/test-plan.md` 模板生成 `output/test-plan.md`。
关键约束：`## 2. 需求条目清单` 下的 requirements 代码块格式必须为 `RID: 描述`，校验脚本依赖它。

### Step 3：生成风险分析

按 qa-test-design 技能的风险方法与 `templates/risk.md` 模板生成 `output/risk.md`。
风险分 >= 10 的项必须完成失效模式分析。

### Step 4：生成测试用例

按 qa-testcase-generator 技能生成 `output/testcases.md`，每条用例为一个 YAML 块，
字段契约见 `templates/testcase.yaml`。

### Step 5：校验（反馈循环）

```bash
python scripts/validate_cases.py output/testcases.md --plan output/test-plan.md
```

- 输出 `OK` 才允许进入下一步。
- 校验失败：按报错逐条修正 testcases.md（或 test-plan.md 的 requirements 块），重新运行，最多重试 3 次；仍失败则停止并向用户报告具体错误。

### Step 6：导出 xlsx

```bash
python scripts/export_xlsx.py output/testcases.md --out output/testcases.xlsx
```

若环境缺少依赖（PyYAML / openpyxl），提示安装：`pip install pyyaml openpyxl`。

## 完成标准

回复用户时列出：4 个产物路径、用例总数、按优先级统计（P0/P1/P2 各多少条）、需求覆盖情况（每个 R 是否被覆盖）、以及需求假设清单（如有）。

## 反模式（禁止）

- 跳过 test-plan/risk 直接写用例——用例必须可追溯到需求条目与风险项
- 自行改变流水线顺序或合并步骤
- 校验未通过就导出 xlsx
- 输出英文产物（除非需求文档本身为英文）
