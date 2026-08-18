---
name: qa-testcase-generator
description: >-
  依据覆盖矩阵（coverage-matrix.md）、测试方案（test-plan.md）与风险分析（risk.md）生成结构化测试用例
  testcases.md，并生成 QA Review（qa-review.md）。每条用例为符合 templates/testcase.schema.yaml 契约的 YAML 块。
  当 qa-orchestrator 流水线执行 Step 6/7，或用户要求生成测试用例、qa-review / 覆盖矩阵追溯时使用。
---

# QA Testcase Generator（用例生成与 QA Review）

输入：`output/coverage-matrix.md`（覆盖契约）、`output/test-plan.md`（需求条目清单 R1..Rn 与技术选型）、`output/risk.md`（风险项与优先级映射）。
输出：`output/testcases.md`，随后必须写 `output/qa-review.md`。输出语言与需求文档一致。

## 输出格式（必须遵守）

testcases.md 结构：

````markdown
# 测试用例：{功能名称}

- 需求来源：output/test-plan.md
- 用例总数：{N}
- 优先级分布：P0 x 条 / P1 y 条 / P2 z 条

## TC-XXX-001 用例标题

```yaml
id: TC-XXX-001
title: 用例标题
priority: P0
type: 功能
preconditions:
  - 前置条件
steps:
  - 步骤 1
  - 步骤 2
expected: 可观察的预期结果
design_method: 边界值
requirement_ref: R1
```
````

每条用例一个小节，标题为 `## {id} {title}`，正文只有一个 yaml 代码块。
字段定义与枚举值以 `templates/testcase.schema.yaml` 为准（示例见 `testcase.example.yaml`），不得增删字段、不得偏离枚举。

## 覆盖规则（生成时逐条自查）

1. **矩阵覆盖**：coverage-matrix.md 每一行至少 1 条用例；每个 R 条目至少 1 条正向用例；`requirement_ref` 必须引用真实存在的 R 编号。
2. **风险覆盖**：risk.md 中 CRITICAL 风险必须有 P0 用例（正常+异常路径），HIGH 风险必须有 P0/P1 用例。
3. **边界覆盖**：所有数值/计数/时效约束必须出边界用例（边界点、略低、略高），`design_method: 边界值`。
4. **状态覆盖**：存在状态流转时，覆盖全部合法转换 + 至少 1 条非法转换。
5. **组合控制**：多维度组合场景按 pairwise 思路只选代表性组合（任意两因子的全部取值对至少出现一次），禁止全排列。
6. **总量控制**：单个功能用例总数一般 10~50 条；超出时优先砍 LOW 风险对应的 P2 用例。

## 用例编写质量要求

- **原子性**：一条用例只验证一个点；步骤与预期一一对应，不写"测试各项功能正常"这类含糊预期。
- **可观察**：expected 必须是可判定的（页面提示、数据变化、接口返回），不写"系统正常处理"。
- **含数据**：步骤中给出具体测试数据（如手机号 13800138000、验证码错误值 000000），不写"输入合法数据"。
- **独立性**：用例之间不互相依赖，前置条件写清。
- **ID 规则**：`TC-<模块缩写>-<3位序号>`，模块缩写取功能英文缩写大写（如 REG、LOGIN），序号连续不重复。

## QA Review（生成用例后必须执行）

用例写完后，按 `templates/qa-review.md` 生成 `output/qa-review.md`：

1. **追溯表**：SC↔TC 映射，结论仅 `COVERED` / `GAP` / `DUPLICATE` / `WEAK`。
2. **Coverage Gap**：未覆盖或弱覆盖的场景。
3. **Test Smell**：重复、含糊预期、不可观察结果等。
4. 用例校验失败可改用例并**整份重出** Review；**不得回写** coverage-matrix.md。

## 反模式

- 用例无法追溯到矩阵行、R 编号或风险项——不可追溯的覆盖是无效覆盖
- 跳过覆盖矩阵或跳过 qa-review.md
- 把多个验证点塞进一条用例
- 预期结果使用"应该没问题"、"正常显示"等不可判定描述
- 组合场景全排列生成上百条用例
- YAML 块外再写大段解释文字（校验脚本只解析 yaml 块）
