---
name: qa-orchestrator
description: >-
  Generates QA test plans, risk analysis, coverage matrix, structured test cases, and QA review
  from PRD/requirement documents, then validates and exports xlsx. Use when the user mentions /qa,
  test plan, test cases, coverage matrix, QA review, QA 测试方案, 测试用例, 覆盖矩阵, PRD testing,
  or requirement-based test design.
---

# QA Orchestrator（测试生成流水线）

固定流水线入口。**严格按顺序执行以下步骤，不跳步、不改序、不做条件分支路由。**

## 路径解析（优先顺序）

1. 工作区根目录存在 `qagent.yaml` → 读取 `input_dir` / `output_dir` / `schema`
2. 否则使用本技能 bundled 配置 [`config.defaults.yaml`](config.defaults.yaml) 与 [`templates/`](templates/)
3. 脚本与 CLI 优先使用已安装的 `qagent` 命令；未安装时使用 [`scripts/run.py`](scripts/run.py)

**Bundled assets：**

- 模板：本目录 `templates/`（或工作区 `templates/`）
- Schema 契约：`templates/testcase.schema.yaml`（唯一事实来源）
- 示例：`templates/testcase.example.yaml`

## 触发方式

- **Web 上传（推荐）**：`qagent serve` → 浏览器上传 PRD/需求文档 → 点击生成
- **命令行上传目录**：文档放入 `input/uploads/` → `qagent run --uploads`
- **Cursor Skill**：`/qa generate`（用户可在对话中 @ 附件文档）
- 自然语言："根据这些需求文档生成测试方案和用例"

## 流水线步骤

```
Task Progress:
- [ ] Step 0: qagent generate <需求文件> --out <输出目录>（初始化流水线状态）
- [ ] Step 1: 解析需求（PRD + 设计文档合并摄入）
- [ ] Step 2: 生成 {output_dir}/test-requirements.md（详细测试需求，穷举可测点）
- [ ] Step 3: 生成 {output_dir}/test-plan.md（基于测试需求）
- [ ] Step 4: 生成 {output_dir}/risk.md
- [ ] Step 5: 生成 {output_dir}/coverage-matrix.md（覆盖契约，先于用例）
- [ ] Step 6: 生成 {output_dir}/testcases.md（基于矩阵 + 测试需求 + 方案 + 风险）
- [ ] Step 7: 生成 {output_dir}/qa-review.md（SC↔TC 追溯与 Gap）
- [ ] Step 8: 运行校验，失败则修正重试
- [ ] Step 9: 导出 {output_dir}/testcases.xlsx
```

### Step 0：初始化

两条入口，择一使用：

**路径 A — 独立 Agent 一键运行（无需 Cursor 分步）：**

```bash
qagent run input/requirement-example.md --out output
```

自动执行 Step 1–9，完成后直接查看产物，无需再手动跟进后续步骤。

**路径 B — Cursor 分步流水线：**

```bash
qagent generate <需求文件> --out <输出目录>
```

初始化流水线状态后，**严格按序执行 Step 1–9**（本 Skill 后续各节）。

### Step 1：解析需求

读取 PRD 与研发设计文档（可合并上传），提炼模块、API、边界与业务规则。用户提供的 `测试需求.md` 作为补充输入合并进源文档。

### Step 2：生成测试需求

从 PRD + 设计文档生成 `{output_dir}/test-requirements.md`（**不是** test-plan，**不是**用例）。
必须包含：功能/API/边界/异常/非功能清单、第 8 节覆盖矩阵（模块 × 测试类型总览，**不是** Step 5 的 `coverage-matrix.md`）、PRE 追溯预备条目。目标是尽量不漏测。

模板：`templates/test-requirements-output.md`

### Step 3：生成测试方案

基于 test-requirements.md，按 qa-test-design 技能与 `templates/test-plan.md` 生成 `{output_dir}/test-plan.md`。
`## 2. 需求条目清单` 下 requirements 块格式必须为 `RID: 描述`；R 与 PRE 条目不得遗漏。
第 5 节下必须含 `### 5.1 测试层级`。

### Step 4：生成风险分析

按 qa-test-design 技能与 `templates/risk.md` 生成 `{output_dir}/risk.md`。
风险分 >= 10 的项必须完成失效模式分析；RK 表格列名与模板保持一致。

### Step 5：生成覆盖矩阵

按 qa-test-design 技能与 `templates/coverage-matrix.md` 生成 `{output_dir}/coverage-matrix.md`。
**没有矩阵禁止写用例。** 矩阵是计划覆盖契约，用例必须按行落地。

### Step 6：生成测试用例

按 qa-testcase-generator 技能生成 `{output_dir}/testcases.md`。
覆盖依据优先级：coverage-matrix.md > test-requirements.md > test-plan R 条目 > risk.md。
字段契约见 `templates/testcase.schema.yaml`。

### Step 7：生成 QA Review

按 qa-testcase-generator 技能与 `templates/qa-review.md` 生成 `{output_dir}/qa-review.md`。
必须含 SC↔TC 追溯表、Coverage Gap、Test Smell；结论仅 `COVERED` / `GAP` / `DUPLICATE` / `WEAK`。

### Step 8：校验（反馈循环）

```bash
qagent check --out output
# 或分步：
qagent validate output/testcases.md --plan output/test-plan.md --risk output/risk.md
```

- 输出 `OK` 才允许进入下一步。
- 校验失败：按报错修正，重新运行，最多重试 3 次（见 `qagent.yaml` 的 `retry_limit`）。
- 矩阵结构失败只修矩阵；用例失败可改用例并重出 Review，不回写矩阵。

### Step 9：导出 xlsx

```bash
qagent export output/testcases.md --out output/testcases.xlsx --plan output/test-plan.md
# 或一步完成 Step 8-9：
qagent pipeline validate-export --out output
```

**必须带 `--plan`**，校验未通过禁止导出（勿使用 `--force`）。

依赖安装：`pip install -e .` 或 `pip install pyyaml openpyxl`。

## 完成标准

回复用户时列出：7 个产物路径（test-requirements.md、test-plan.md、risk.md、coverage-matrix.md、testcases.md、qa-review.md、testcases.xlsx）、用例总数、P0/P1/P2 统计、需求覆盖、需求假设清单（如有）。

## 反模式（禁止）

- 跳过 test-requirements / test-plan / risk 直接写用例
- 跳过覆盖矩阵直接写用例
- 自行改变流水线顺序或合并步骤
- 校验未通过就导出 xlsx
- 输出英文产物（除非需求文档本身为英文）
