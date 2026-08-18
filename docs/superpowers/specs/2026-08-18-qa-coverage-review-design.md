# QAgent 覆盖矩阵与 QA Review 设计

日期：2026-08-18  
状态：待实现  
范围：把开源 QA Skills 的「先矩阵、后用例、再审查」吸收进现有独立 Agent，不停在用例 + xlsx。

## 1. 背景与目标

QAgent 已有固定流水线：

```text
PRD / 设计 → test-requirements.md → test-plan.md → risk.md
           → testcases.md → 校验 / LLM 修正 → testcases.xlsx
```

对照 petrkindlmann/qa-skills（`ai-test-generation` / `ai-qa-review` / `test-strategy`）、Anthropic Testing Strategy、QIOS 后，本期只吸收三块方法学：

1. **Coverage Matrix 作为一等产物**：没有矩阵禁止写用例。
2. **测试层级**：在 test-plan 中写明 API / UI-E2E / 安全 / 性能（无代码仓库时 Unit 标「否」）。
3. **QA Review**：用例生成后做 SC↔TC 追溯、Coverage Gap、Test Smell。

本期明确不做：Gherkin、Playwright / Cypress / Postman、浏览器探索、独立 `qa-test-engineer` 技能包。方法并入现有 `qa-orchestrator` / `qa-test-design` / `qa-testcase-generator`。

## 2. 流水线

独立 Agent（`qagent run`）与 Cursor Skill 共用同一顺序。

| 步 | 产物 | 角色 |
|----|------|------|
| 2 | `test-requirements.md` | 穷举可测点（现有）。第 8 节仍是模块×类型总览 |
| 3 | `test-plan.md` | 现有 R 编号 + 策略；第 5 节下新增 `### 5.1 测试层级` |
| 4 | `risk.md` | 现有 5×5 |
| 5 | `coverage-matrix.md` | **计划覆盖契约**，驱动用例 |
| 6 | `testcases.md` | 矩阵每一行至少 1 条用例 |
| 7 | `qa-review.md` | 回填 SC↔TC、Gap、Smell |
| 8 | 校验 | Schema / R 覆盖 / 风险 / 矩阵行覆盖 |
| 9 | `testcases.xlsx` | 现有导出，列不变 |

`PipelineStep` 新增两个枚举值，插在现有步骤之间：

- `COVERAGE_MATRIX`：在 `RISK` 之后、`TESTCASES` 之前
- `QA_REVIEW`：在 `TESTCASES` 之后、`VALIDATE` 之前

现有步骤编号对外文案改为「共 9 步」（含解析与导出）。内部枚举名保持字符串稳定，避免破坏已有 `.qagent-pipeline.json` 中已完成的旧步骤名。

## 3. 产物契约

### 3.1 `coverage-matrix.md`

模板：`templates/coverage-matrix.md`，并复制到 `skills/qa-orchestrator/templates/`。

必含章节：

```markdown
# 覆盖矩阵：{功能名称}

## 1. 覆盖契约

| 场景ID | 需求 | 场景 | 类别 | 优先级 | 判定方式 |
|--------|------|------|------|--------|----------|
| SC-001 | R1 | … | Happy | P0 | … |

## 2. 覆盖规则自检
## 3. 测试层级建议
```

解析器只认 **「## 1. 覆盖契约」后的第一张表**。列名必须完全一致。

字段规则：

| 列 | 规则 |
|----|------|
| 场景ID | `^SC-\d{3}$`，全文唯一 |
| 需求 | 必须是 `test-plan.md` ```requirements``` 块中已有的 R |
| 场景 | 非空，一条一行，禁止重复场景 |
| 类别 | 仅 `Happy` / `Boundary` / `Negative` / `Security` / `State` / `Concurrency` |
| 优先级 | `P0` / `P1` / `P2` |
| 判定方式 | 可观察结果（状态码、文案、数据变化），禁止「成功/正常」 |

覆盖规则分两层：

- **脚本在矩阵阶段检查：** 每个 R 至少 1 行；`SC-NNN` / 类别 / 优先级合法；需求 R 必须存在。
- **仅 prompt 约束、脚本不查：** 有边界的 R 应有 `Boundary` 或 `Negative`；每个 API 应有成功（`Happy`）+ 失败（`Negative`）；CRITICAL / HIGH 风险关联的 R 应有矩阵行。风险优先级仍由现有 `validate_risk_coverage` 在用例阶段检查。

文档没有 a11y 要求时，不编造 Accessibility 行。不新增 `Accessibility` 类别。

### 3.2 `qa-review.md`

模板：`templates/qa-review.md`，并复制到 `skills/qa-orchestrator/templates/`。

必含章节：

```markdown
# QA Review：{功能名称}

## 1. 追溯表（SC ↔ TC）

| 场景ID | 对应用例 | 结论 |
|--------|----------|------|
| SC-001 | TC-OCR-001 | COVERED |
| SC-002 | — | GAP |

## 2. Coverage Gap
## 3. Test Smell
## 4. 评审摘要
```

解析器只认 **「## 1. 追溯表」后的第一张表**。列名必须完全一致。

| 列 | 规则 |
|----|------|
| 场景ID | 必须出现在矩阵中 |
| 对应用例 | `COVERED` / `DUPLICATE` 时必须是 `testcases.md` 中存在的 `TC-…`；`GAP` 时写 `—` |
| 结论 | 仅 `COVERED` / `GAP` / `DUPLICATE` / `WEAK` |

Smell 类型（第 3 节表格，脚本不强制枚举，只当 warning 来源）：模糊预期、一条多断言、编造 API、重复场景、缺测试数据、不可判定。

### 3.3 现有产物只做加法

- **`templates/testcase.schema.yaml`：不增删字段。** 不增加 `coverage_ref`。xlsx 导出列不变。
- `test-plan.md`：在「## 5. 测试类型与策略」下增加 `### 5.1 测试层级` 表，列：`层级 | 是否覆盖 | 覆盖目标 | 对应矩阵类别`。层级固定为 API、UI/E2E、安全、性能；无代码仓库时不写 Unit，或写一行「Unit | 否 | 无被测代码 | —」。
- `plan_required_sections` **不增加** `### 5.1`，避免旧 fixture / 旧产物突然校验失败。prompt 要求新生成必须带这一小节。
- `test-requirements.md` 第 8 节保留；细粒度场景改由矩阵负责。
- `testcases.md`：每条用例的 `expected` 必须能对应其覆盖的矩阵行「判定方式」；禁止生成矩阵中不存在的孤立场景（prompt 约束，脚本通过「矩阵行必须被追溯」反向保证）。

## 4. Prompt 与运行器

`qagent/agent/prompts.py`：

- 新增 `build_coverage_matrix_prompt(test_requirements, test_plan, risk, config)`
- 新增 `build_qa_review_prompt(matrix, testcases, test_plan, risk, config)`
- `build_test_plan_prompt`：要求输出 `### 5.1 测试层级`
- `build_testcases_prompt`：输入增加矩阵全文；硬性要求「矩阵每一行至少 1 条用例，禁止无矩阵行的用例」
- `build_fix_prompt`：输入增加矩阵未覆盖行 + Review 中结论为 `GAP` 的行
- `SYSTEM` 流水线描述改为含矩阵与 Review

`qagent/agent/llm.py` `MockLLM` 的 `task_markers` 增加：

- `"生成完整的 coverage-matrix.md"` → `coverage-matrix`
- `"生成完整的 qa-review.md"` → `qa-review`

关键词必须写进对应 user prompt，保证 mock 可匹配。

`qagent/agent/runner.py` 顺序：

```text
test-requirements → test-plan → risk
  → coverage-matrix（结构坏则只修矩阵）
  → testcases
  → qa-review
  → validate（失败则只改 testcases，然后整份重出 qa-review）
  → export
```

`QAgentConfig` 新增只读路径：

- `coverage_matrix_path` → `{output_dir}/coverage-matrix.md`
- `qa_review_path` → `{output_dir}/qa-review.md`

`RunResult.artifacts` 增加 `coverage_matrix`、`qa_review`。

## 5. 解析与校验

`qagent/parsing.py`：

- `CoverageRow`：`scenario_id, requirement_id, scenario, category, priority, oracle`
- `ReviewTraceRow`：`scenario_id, case_id, verdict`
- `parse_coverage_matrix(path) -> list[CoverageRow]`
- `parse_review_trace(path) -> list[ReviewTraceRow]`

表格解析复用 `parse_risks` 的行拆分方式（`|` 分隔，跳过表头与 `---`）。

`qagent/validation.py`：

`validate_matrix(rows, requirement_ids, config) -> (errors, warnings)`

- 无行 → error
- `SC-NNN` 非法或重复 → error
- 类别 / 优先级非法 → error
- 需求不在 `requirement_ids` → error
- 某个 R 零行 → `strict_coverage` 时 error，否则 warning

`validate_review_trace(rows, matrix_ids, case_ids, config) -> (errors, warnings)`

- 矩阵每个 SC 必须在追溯表出现，缺行 → error
- 追溯表出现未知 SC → error
- `COVERED` / `DUPLICATE`：`case_id` 必须在 `case_ids` 中，否则 error。`DUPLICATE` 视为该 SC 已覆盖（不记 GAP），重复问题只在 Smell 中出现
- `GAP`：`strict_coverage` 时 error，否则 warning
- `WEAK`：视为已覆盖但质量差，只 warning
- 结论非法 → error

现有 `validate_cases`、`validate_plan_structure`、`validate_risk_coverage` 行为不变。

`qagent check` / `_run_validate` / `QAgentRunner._full_validate` 在已有校验之后追加矩阵与 Review。缺少这两个文件视为 error（与缺 test-plan 相同）。

`pipeline.check_prerequisites`：

- 进入 `TESTCASES` 必须已有 `coverage-matrix.md`
- 进入 `VALIDATE` / `EXPORT` 必须已有矩阵与 Review（与 test-plan / testcases 并列）

## 6. 失败与重试

`retry_limit`（默认 3，含首次）沿用 `qagent.yaml`。

| 阶段 | 失败 | 处理 |
|------|------|------|
| 矩阵生成 | 无契约表 / 无 `SC-` / R 不存在 / 类别非法 / 某 R 零行 | **只修矩阵**，最多 `retry_limit` 次；仍失败则停止，不写用例 |
| 用例 + Review | Schema、R 覆盖、风险、追溯 `GAP`（strict）、COVERED 但 TC 不存在 | **只改 testcases.md**，然后**整份重出** qa-review.md；不改矩阵 |
| Smell / `WEAK` | — | warning，不阻断导出 |
| LLM / 网络 | `RuntimeError` | 直接失败，与现网一致 |

矩阵是契约：用例修正循环不得回写 `coverage-matrix.md`。

## 7. CLI / Skill / 文档

- `qagent check`：产物清单增加 `coverage-matrix.md`、`qa-review.md`
- `qagent run --mock`：Mock 响应增加两个 fixture
- `qagent pipeline status`：通过新增 `PipelineStep` 自动露出两步
- Web `serve`：`artifacts` 多两个文件名，协议不改
- `AGENT.md`、`README.md`：流水线改为含矩阵与 Review
- `skills/qa-orchestrator/SKILL.md`：步骤表同步；bundled `templates/` 放入两份新模板
- `skills/qa-test-design/SKILL.md`：增加「先写 coverage-matrix」与测试层级
- `skills/qa-testcase-generator/SKILL.md`：按矩阵生成；生成后必须出 qa-review

## 8. 测试

不调用真实 LLM。

新增 fixture（对齐现有 `R1/R2/R3` 与 `testcases-valid.md`，不改旧用例夹具内容）：

- `tests/fixtures/coverage-matrix.md`：至少覆盖 R1/R2/R3，类别合法
- `tests/fixtures/qa-review.md`：每个 SC 均为 `COVERED`，TC 与 `testcases-valid.md` 中 id 一致
- `tests/fixtures/coverage-matrix-bad.md`：非法类别或引用不存在的 R
- `tests/fixtures/qa-review-gap.md`：含 `GAP` 行

`tests/test_qa_common.py`（或同目录新文件 `tests/test_coverage_review.py`）覆盖：

- 解析合法矩阵与合法追溯表
- 非法类别、不存在的 R → error
- `COVERED` 但 TC 不存在 → error
- `strict_coverage=True` 时 `GAP` → error
- `WEAK` → warning 且无 error

`tests/test_agent.py`：

- mock 响应增加矩阵与 Review
- 断言输出目录存在这两个文件
- `artifacts` 含 `coverage_matrix`、`qa_review`
- LLM 调用次数 ≥ 6（需求 / 方案 / 风险 / 矩阵 / 用例 / Review）

## 9. 完成标准

同时成立才算完成：

1. `qagent run --mock` 成功，输出 7 个产物：`test-requirements.md`、`test-plan.md`、`risk.md`、`coverage-matrix.md`、`testcases.md`、`qa-review.md`、`testcases.xlsx`
2. `pytest` 全绿
3. `qagent check` 会校验矩阵行与 SC↔TC
4. 未改 `testcase.schema.yaml` 的 fields / export_columns
5. 未引入 Gherkin / Playwright / 新技能包
6. AGENT / README / 三条 Skill 与实现一致

## 10. 影响文件清单

新增：

- `templates/coverage-matrix.md`
- `templates/qa-review.md`
- `skills/qa-orchestrator/templates/coverage-matrix.md`
- `skills/qa-orchestrator/templates/qa-review.md`
- `tests/fixtures/coverage-matrix.md`
- `tests/fixtures/qa-review.md`
- `tests/fixtures/coverage-matrix-bad.md`
- `tests/fixtures/qa-review-gap.md`

修改：

- `qagent/config.py`
- `qagent/pipeline.py`
- `qagent/agent/prompts.py`
- `qagent/agent/llm.py`
- `qagent/agent/runner.py`
- `qagent/parsing.py`
- `qagent/validation.py`
- `qagent/cli.py`
- `tests/test_agent.py`
- `tests/test_qa_common.py`（或新增 `tests/test_coverage_review.py`）
- `AGENT.md`
- `README.md`
- `skills/qa-orchestrator/SKILL.md`
- `skills/qa-test-design/SKILL.md`
- `skills/qa-testcase-generator/SKILL.md`
- `templates/test-plan.md`（增加 5.1 示例，不改必填章节列表）

不改：`qagent/ingest.py`、`qagent/exporters/`、`templates/testcase.schema.yaml` 字段、xlsx 列。
