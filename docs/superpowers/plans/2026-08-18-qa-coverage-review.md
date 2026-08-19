# Coverage Matrix + QA Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有 QAgent 流水线中插入一等产物 `coverage-matrix.md` 与 `qa-review.md`，做到先矩阵后用例、用例后再审查，且不改用例 Schema。

**Architecture:** 解析与校验走脚本（对齐 `parse_risks` / `validate_risk_coverage`）；LLM 只负责按模板生成/修正 Markdown。矩阵在用例之前生成并结构校验，是不可回写的契约；用例校验失败只改 `testcases.md` 并整份重出 Review。独立 Agent 与 Cursor Skill 共用同一 `PipelineStep` 顺序。

**Tech Stack:** Python 3、pytest、现有 `qagent` 包（PyYAML、openpyxl）。不新增依赖。

**Spec:** `docs/superpowers/specs/2026-08-18-qa-coverage-review-design.md`

## Global Constraints

- 不改 `templates/testcase.schema.yaml` 的 `fields` / `export_columns`，不增加 `coverage_ref`
- 不引入 Gherkin / Playwright / Cypress / Postman，不新建 Skill 包
- `plan_required_sections` 不增加 `### 5.1`
- 矩阵类别仅 `Happy` / `Boundary` / `Negative` / `Security` / `State` / `Concurrency`
- Review 结论仅 `COVERED` / `GAP` / `DUPLICATE` / `WEAK`
- 场景ID `^SC-\d{3}$`；`strict_coverage` 时 R 零行与 `GAP` 为 error
- `MockLLM` 匹配短语必须是 `生成完整的 coverage-matrix.md` 与 `生成完整的 qa-review.md`
- 旧 `.qagent-pipeline.json` 步骤字符串保持稳定：只新增 `coverage_matrix`、`qa_review`

## File Map

| 文件 | 职责 |
|------|------|
| `qagent/parsing.py` | `CoverageRow` / `ReviewTraceRow` / `parse_coverage_matrix` / `parse_review_trace` |
| `qagent/validation.py` | `validate_matrix` / `validate_review_trace` |
| `qagent/config.py` | `coverage_matrix_path` / `qa_review_path` |
| `qagent/pipeline.py` | 新步骤与产物映射、前置检查 |
| `templates/coverage-matrix.md` | 矩阵模板（根目录） |
| `templates/qa-review.md` | Review 模板（根目录） |
| `templates/test-plan.md` | 增加 `### 5.1 测试层级` 示例 |
| `skills/qa-orchestrator/templates/*` | 与根模板同步的两份新文件 + test-plan 5.1 |
| `qagent/agent/prompts.py` | 矩阵 / Review / 修矩阵 prompt；方案/用例/修用例加约束 |
| `qagent/agent/llm.py` | Mock 关键词 |
| `qagent/agent/runner.py` | 9 步顺序、矩阵重试、用例失败后重出 Review |
| `qagent/cli.py` | `check` / `--mock` / validate 接入新产物 |
| `tests/test_coverage_review.py` | 解析与校验单测 |
| `tests/test_agent.py` | Mock 全流程 |
| `AGENT.md` / `README.md` / 三条 `SKILL.md` | 文档与步骤对齐 |

---

### Task 1: 解析覆盖矩阵与追溯表

**Files:**
- Create: `tests/fixtures/coverage-matrix.md`
- Create: `tests/fixtures/qa-review.md`
- Create: `tests/fixtures/coverage-matrix-bad.md`
- Create: `tests/fixtures/qa-review-gap.md`
- Create: `tests/test_coverage_review.py`
- Modify: `qagent/parsing.py`

**Interfaces:**
- Consumes: `qagent/parsing.py` 中已有 `_parse_table_row`
- Produces:
  - `@dataclass CoverageRow`: `scenario_id: str`, `requirement_id: str`, `scenario: str`, `category: str`, `priority: str`, `oracle: str`
  - `@dataclass ReviewTraceRow`: `scenario_id: str`, `case_id: str`, `verdict: str`
  - `parse_coverage_matrix(path: Path) -> list[CoverageRow]`
  - `parse_review_trace(path: Path) -> list[ReviewTraceRow]`
  - `parse_coverage_matrix` 只解析 `## 1. 覆盖契约` 后第一张表
  - `parse_review_trace` 只解析标题含 `追溯表` 的 `## 1.` 节后第一张表（标题可以是 `## 1. 追溯表（SC ↔ TC）`）

- [ ] **Step 1: 写四个 fixture**

`tests/fixtures/coverage-matrix.md`（必须覆盖 R1/R2/R3，ID 与 `testcases-valid.md` 可对齐）：

```markdown
# 覆盖矩阵：注册

## 1. 覆盖契约

| 场景ID | 需求 | 场景 | 类别 | 优先级 | 判定方式 |
|--------|------|------|------|--------|----------|
| SC-001 | R1 | 未注册手机号正确注册 | Happy | P0 | 注册成功并可登录 |
| SC-002 | R3 | 验证码连续错误 5 次锁定 | Boundary | P0 | 注册操作被锁定 |
| SC-003 | R2 | 验证码超过 5 分钟提交 | Boundary | P1 | 提示验证码过期 |

## 2. 覆盖规则自检

- [x] 每个 R 至少 1 行

## 3. 测试层级建议

| 层级 | 是否覆盖 | 覆盖目标 | 对应矩阵类别 |
|------|---------|---------|-------------|
| API | 否 | 无接口文档 | — |

## 4. 干扰表（解析器必须忽略）

| 场景ID | 需求 | 场景 | 类别 | 优先级 | 判定方式 |
|--------|------|------|------|--------|----------|
| SC-999 | R1 | 不该被解析 | Happy | P0 | x |
```

`tests/fixtures/qa-review.md`：

```markdown
# QA Review：注册

## 1. 追溯表（SC ↔ TC）

| 场景ID | 对应用例 | 结论 |
|--------|----------|------|
| SC-001 | TC-REG-001 | COVERED |
| SC-002 | TC-REG-002 | COVERED |
| SC-003 | TC-REG-003 | COVERED |

## 2. Coverage Gap

无

## 3. Test Smell

| 用例 | Smell | 说明 |
|------|-------|------|
| TC-REG-001 | 模糊预期 | fixture 预期较粗，仅作样例 |

## 4. 评审摘要

3 行全部 COVERED
```

`tests/fixtures/coverage-matrix-bad.md`：

```markdown
# 覆盖矩阵：坏例

## 1. 覆盖契约

| 场景ID | 需求 | 场景 | 类别 | 优先级 | 判定方式 |
|--------|------|------|------|--------|----------|
| SC-001 | R1 | 正确注册 | Happy | P0 | 注册成功 |
| SC-002 | R99 | 不存在的需求 | Foo | P0 | x |
```

`tests/fixtures/qa-review-gap.md`：

```markdown
# QA Review：缺口

## 1. 追溯表（SC ↔ TC）

| 场景ID | 对应用例 | 结论 |
|--------|----------|------|
| SC-001 | TC-REG-001 | COVERED |
| SC-002 | — | GAP |
| SC-003 | TC-REG-003 | COVERED |
```

- [ ] **Step 2: 写失败单测**

在 `tests/test_coverage_review.py`：

```python
from pathlib import Path

from qagent.parsing import parse_coverage_matrix, parse_review_trace

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_coverage_matrix_valid():
    rows = parse_coverage_matrix(FIXTURES / "coverage-matrix.md")
    assert [r.scenario_id for r in rows] == ["SC-001", "SC-002", "SC-003"]
    assert {r.requirement_id for r in rows} == {"R1", "R2", "R3"}
    assert rows[0].category == "Happy"
    assert rows[0].oracle == "注册成功并可登录"
    assert all(r.scenario_id != "SC-999" for r in rows)


def test_parse_review_trace_valid():
    rows = parse_review_trace(FIXTURES / "qa-review.md")
    assert [r.scenario_id for r in rows] == ["SC-001", "SC-002", "SC-003"]
    assert rows[0].case_id == "TC-REG-001"
    assert rows[0].verdict == "COVERED"
```

- [ ] **Step 3: 跑测试确认失败**

Run: `pytest tests/test_coverage_review.py::test_parse_coverage_matrix_valid tests/test_coverage_review.py::test_parse_review_trace_valid -v`

Expected: FAIL，`ImportError` 或 `cannot import name parse_coverage_matrix`

- [ ] **Step 4: 实现解析**

在 `qagent/parsing.py` 增加（复用已有 `_parse_table_row`）：

```python
@dataclass
class CoverageRow:
    scenario_id: str
    requirement_id: str
    scenario: str
    category: str
    priority: str
    oracle: str


@dataclass
class ReviewTraceRow:
    scenario_id: str
    case_id: str
    verdict: str


def _table_after_heading(text: str, heading_prefix: str) -> list[list[str]]:
    """返回指定标题之后第一张 Markdown 表的数据行（不含表头与分隔行）。"""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith(heading_prefix):
            start = i + 1
            break
    if start is None:
        raise ValueError(f"缺少章节: {heading_prefix}")

    rows: list[list[str]] = []
    in_table = False
    for line in lines[start:]:
        stripped = line.strip()
        if stripped.startswith("#"):
            if in_table:
                break
            continue
        if not stripped.startswith("|"):
            if in_table:
                break
            continue
        cells = _parse_table_row(stripped)
        if not cells:
            continue
        first = cells[0]
        if first in ("场景ID", "---", "----") or set(first) <= {"-", ":"}:
            in_table = True
            continue
        if not in_table:
            continue
        rows.append(cells)
    if not in_table:
        raise ValueError(f"{heading_prefix} 后没有表格")
    return rows


def parse_coverage_matrix(path: Path) -> list[CoverageRow]:
    text = path.read_text(encoding="utf-8")
    table = _table_after_heading(text, "## 1. 覆盖契约")
    rows: list[CoverageRow] = []
    for cells in table:
        if len(cells) < 6:
            raise ValueError(f"覆盖契约行列数不足: {cells}")
        rows.append(CoverageRow(
            scenario_id=cells[0],
            requirement_id=cells[1],
            scenario=cells[2],
            category=cells[3],
            priority=cells[4],
            oracle=cells[5],
        ))
    return rows


def parse_review_trace(path: Path) -> list[ReviewTraceRow]:
    text = path.read_text(encoding="utf-8")
    table = _table_after_heading(text, "## 1. 追溯表")
    rows: list[ReviewTraceRow] = []
    for cells in table:
        if len(cells) < 3:
            raise ValueError(f"追溯表行列数不足: {cells}")
        rows.append(ReviewTraceRow(
            scenario_id=cells[0],
            case_id=cells[1],
            verdict=cells[2].upper(),
        ))
    return rows
```

注意：`heading_prefix="## 1. 追溯表"` 能匹配 `## 1. 追溯表（SC ↔ TC）`。分隔行检测要覆盖 `|---|` 这种 `---`。

- [ ] **Step 5: 跑测试确认通过**

Run: `pytest tests/test_coverage_review.py::test_parse_coverage_matrix_valid tests/test_coverage_review.py::test_parse_review_trace_valid -v`

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add tests/fixtures/coverage-matrix.md tests/fixtures/qa-review.md \
  tests/fixtures/coverage-matrix-bad.md tests/fixtures/qa-review-gap.md \
  tests/test_coverage_review.py qagent/parsing.py
git commit -m "$(cat <<'EOF'
feat: parse coverage matrix and QA review trace tables

EOF
)"
```

---

### Task 2: 校验矩阵与追溯表

**Files:**
- Modify: `qagent/validation.py`
- Modify: `tests/test_coverage_review.py`

**Interfaces:**
- Consumes: `CoverageRow`, `ReviewTraceRow`, `QAgentConfig.strict_coverage`
- Produces:
  - `MATRIX_CATEGORIES = {"Happy", "Boundary", "Negative", "Security", "State", "Concurrency"}`
  - `REVIEW_VERDICTS = {"COVERED", "GAP", "DUPLICATE", "WEAK"}`
  - `SC_ID_RE = re.compile(r"^SC-\d{3}$")`
  - `validate_matrix(rows: list[CoverageRow], requirement_ids: set[str], config: QAgentConfig | None = None) -> tuple[list[str], list[str]]`
  - `validate_review_trace(rows: list[ReviewTraceRow], matrix_ids: set[str], case_ids: set[str], config: QAgentConfig | None = None) -> tuple[list[str], list[str]]`

规则（必须按此实现，不得增减）：

`validate_matrix`

- `rows` 空 → error `"覆盖矩阵没有任何场景行"`
- `scenario_id` 不匹配 `SC-NNN` 或重复 → error
- `category` 不在 `MATRIX_CATEGORIES` → error
- `priority` 不在 `{P0,P1,P2}` → error
- `requirement_id` 不在 `requirement_ids` → error
- 某个 `requirement_ids` 中的 R 零行 → `strict_coverage` 时 error，否则 warning；文案含该 R

`validate_review_trace`

- 矩阵每个 SC 必须出现在 `rows`，缺行 → error
- `rows` 出现未知 SC → error
- 结论不在 `REVIEW_VERDICTS` → error
- `COVERED` / `DUPLICATE` / `WEAK`：`case_id` 必须在 `case_ids`（`WEAK` 视为已覆盖）；缺失 → error
- `GAP`：`strict_coverage` 时 error，否则 warning
- `DUPLICATE` 不记 GAP
- `WEAK` 只 warning（例如 `"{id} 结论为 WEAK"`），无 error

- [ ] **Step 1: 写失败单测**

追加到 `tests/test_coverage_review.py`：

```python
from qagent.config import QAgentConfig
from qagent.parsing import parse_coverage_matrix, parse_review_trace
from qagent.validation import validate_matrix, validate_review_trace

REPO = Path(__file__).resolve().parents[1]
SCHEMA = REPO / "templates" / "testcase.schema.yaml"


def _cfg(strict: bool) -> QAgentConfig:
    return QAgentConfig(
        workspace=REPO,
        input_dir=REPO / "input",
        output_dir=REPO / "output",
        schema_path=SCHEMA,
        strict_coverage=strict,
    )


def test_validate_matrix_rejects_bad_category_and_missing_r():
    rows = parse_coverage_matrix(FIXTURES / "coverage-matrix-bad.md")
    errors, _ = validate_matrix(rows, {"R1", "R2", "R3"}, _cfg(True))
    assert errors
    assert any("Foo" in e or "类别" in e for e in errors)
    assert any("R99" in e for e in errors)


def test_validate_matrix_strict_uncovered_requirement():
    rows = parse_coverage_matrix(FIXTURES / "coverage-matrix.md")
    errors, warnings = validate_matrix(rows, {"R1", "R2", "R3", "R4"}, _cfg(True))
    assert any("R4" in e for e in errors)
    errors2, warnings2 = validate_matrix(rows, {"R1", "R2", "R3", "R4"}, _cfg(False))
    assert not any("R4" in e for e in errors2)
    assert any("R4" in w for w in warnings2)


def test_validate_review_gap_strict():
    rows = parse_review_trace(FIXTURES / "qa-review-gap.md")
    matrix_ids = {"SC-001", "SC-002", "SC-003"}
    case_ids = {"TC-REG-001", "TC-REG-002", "TC-REG-003"}
    errors, _ = validate_review_trace(rows, matrix_ids, case_ids, _cfg(True))
    assert any("GAP" in e or "SC-002" in e for e in errors)


def test_validate_review_covered_unknown_case():
    rows = parse_review_trace(FIXTURES / "qa-review.md")
    rows[0].case_id = "TC-NOPE-001"
    errors, _ = validate_review_trace(
        rows, {"SC-001", "SC-002", "SC-003"},
        {"TC-REG-001", "TC-REG-002", "TC-REG-003"},
        _cfg(True),
    )
    assert any("TC-NOPE-001" in e for e in errors)


def test_validate_review_weak_is_warning():
    from qagent.parsing import ReviewTraceRow
    rows = [
        ReviewTraceRow("SC-001", "TC-REG-001", "WEAK"),
    ]
    errors, warnings = validate_review_trace(
        rows, {"SC-001"}, {"TC-REG-001"}, _cfg(True),
    )
    assert not errors
    assert warnings
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_coverage_review.py -k validate -v`

Expected: FAIL，`cannot import name validate_matrix`

- [ ] **Step 3: 实现校验**

在 `qagent/validation.py` 增加 import：`re`、`CoverageRow`、`ReviewTraceRow`。实现：

```python
import re

from qagent.parsing import CoverageRow, ReviewTraceRow, RiskItem, parse_risks, ref_ids

MATRIX_CATEGORIES = {"Happy", "Boundary", "Negative", "Security", "State", "Concurrency"}
REVIEW_VERDICTS = {"COVERED", "GAP", "DUPLICATE", "WEAK"}
SC_ID_RE = re.compile(r"^SC-\d{3}$")


def validate_matrix(
    rows: list[CoverageRow],
    requirement_ids: set[str],
    config: QAgentConfig | None = None,
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    strict = config.strict_coverage if config else False
    if not rows:
        return ["覆盖矩阵没有任何场景行"], warnings

    seen: set[str] = set()
    covered: set[str] = set()
    for row in rows:
        sid = row.scenario_id
        if not SC_ID_RE.match(sid):
            errors.append(f"{sid}: 场景ID 不符合 SC-NNN")
        if sid in seen:
            errors.append(f"{sid}: 场景ID 重复")
        seen.add(sid)
        if row.category not in MATRIX_CATEGORIES:
            errors.append(f"{sid}: 类别 {row.category!r} 不合法")
        if row.priority not in {"P0", "P1", "P2"}:
            errors.append(f"{sid}: 优先级 {row.priority!r} 不合法")
        if row.requirement_id not in requirement_ids:
            errors.append(f"{sid}: 需求 {row.requirement_id} 不存在")
        else:
            covered.add(row.requirement_id)

    for rid in sorted(requirement_ids - covered):
        msg = f"需求 {rid} 在覆盖矩阵中没有场景行"
        if strict:
            errors.append(msg)
        else:
            warnings.append(msg)
    return errors, warnings


def validate_review_trace(
    rows: list[ReviewTraceRow],
    matrix_ids: set[str],
    case_ids: set[str],
    config: QAgentConfig | None = None,
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    strict = config.strict_coverage if config else False
    present = {r.scenario_id for r in rows}

    for sid in sorted(matrix_ids - present):
        errors.append(f"{sid}: 追溯表缺失")
    for row in rows:
        if row.scenario_id not in matrix_ids:
            errors.append(f"{row.scenario_id}: 追溯表出现未知场景")
        if row.verdict not in REVIEW_VERDICTS:
            errors.append(f"{row.scenario_id}: 结论 {row.verdict!r} 不合法")
            continue
        if row.verdict == "GAP":
            msg = f"{row.scenario_id}: 结论为 GAP"
            if strict:
                errors.append(msg)
            else:
                warnings.append(msg)
            continue
        if row.case_id not in case_ids:
            errors.append(f"{row.scenario_id}: 用例 {row.case_id} 不存在")
        if row.verdict == "WEAK":
            warnings.append(f"{row.scenario_id}: 结论为 WEAK")
    return errors, warnings
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_coverage_review.py -v`

Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add qagent/validation.py tests/test_coverage_review.py
git commit -m "$(cat <<'EOF'
feat: validate coverage matrix rows and SC-TC trace

EOF
)"
```

---

### Task 3: 配置路径与流水线步骤

**Files:**
- Modify: `qagent/config.py`
- Modify: `qagent/pipeline.py`
- Modify: `tests/test_coverage_review.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `QAgentConfig.coverage_matrix_path` → `{output_dir}/coverage-matrix.md`
  - `QAgentConfig.qa_review_path` → `{output_dir}/qa-review.md`
  - `PipelineStep.COVERAGE_MATRIX = "coverage_matrix"`（夹在 `risk` 与 `testcases` 之间）
  - `PipelineStep.QA_REVIEW = "qa_review"`（夹在 `testcases` 与 `validate` 之间）
  - `_artifact_for_step` 映射这两个步骤
  - `check_prerequisites(TESTCASES)` 在缺少矩阵文件时报错
  - `check_prerequisites(VALIDATE)` 与 `EXPORT` 的显式文件清单包含矩阵与 Review

`PipelineStep` 最终顺序必须是：

```python
class PipelineStep(str, Enum):
    PARSE_REQUIREMENT = "parse_requirement"
    TEST_REQUIREMENTS = "test_requirements"
    TEST_PLAN = "test_plan"
    RISK = "risk"
    COVERAGE_MATRIX = "coverage_matrix"
    TESTCASES = "testcases"
    QA_REVIEW = "qa_review"
    VALIDATE = "validate"
    EXPORT = "export"
```

`_artifact_for_step` 增加：

```python
PipelineStep.COVERAGE_MATRIX: config.coverage_matrix_path,
PipelineStep.QA_REVIEW: config.qa_review_path,
```

`check_prerequisites` 里 `VALIDATE` / `EXPORT` 的 for-path 列表改为：

```python
(
    config.test_requirements_path,
    config.test_plan_path,
    config.coverage_matrix_path,
    config.testcases_path,
    config.qa_review_path,
)
```

- [ ] **Step 1: 写失败单测**

```python
from qagent.config import QAgentConfig
from qagent.pipeline import PipelineStep, check_prerequisites


def test_config_artifact_paths(tmp_path):
    cfg = QAgentConfig(
        workspace=REPO, input_dir=tmp_path, output_dir=tmp_path / "out",
        schema_path=SCHEMA,
    )
    assert cfg.coverage_matrix_path == tmp_path / "out" / "coverage-matrix.md"
    assert cfg.qa_review_path == tmp_path / "out" / "qa-review.md"


def test_testcases_requires_matrix(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    cfg = QAgentConfig(
        workspace=REPO, input_dir=tmp_path, output_dir=out, schema_path=SCHEMA,
    )
    (out / "test-requirements.md").write_text("x", encoding="utf-8")
    (out / "test-plan.md").write_text("x", encoding="utf-8")
    (out / "risk.md").write_text("x", encoding="utf-8")
    errors = check_prerequisites(cfg, PipelineStep.TESTCASES)
    assert any("coverage-matrix" in e for e in errors)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_coverage_review.py::test_config_artifact_paths tests/test_coverage_review.py::test_testcases_requires_matrix -v`

Expected: FAIL（属性不存在或前置检查不报矩阵缺失）

- [ ] **Step 3: 改 config 与 pipeline**

`qagent/config.py` 的 `QAgentConfig` 增加：

```python
@property
def coverage_matrix_path(self) -> Path:
    return self.output_dir / "coverage-matrix.md"

@property
def qa_review_path(self) -> Path:
    return self.output_dir / "qa-review.md"
```

按上面改 `pipeline.py`。不要重命名旧枚举值。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_coverage_review.py tests/test_qa_common.py tests/test_agent.py -v`

Expected: `test_coverage_review` 新测试 PASS；旧测试若因 `PipelineStep` 顺序变化而失败则只修映射，不改旧 fixture。`test_agent` 此时仍可能失败（runner 尚未接入），**本任务不改 runner**。若 `test_agent` 因枚举变化失败，先记下，Task 6 修。预期 `test_agent` 仍走旧 runner，应继续 PASS。

- [ ] **Step 5: Commit**

```bash
git add qagent/config.py qagent/pipeline.py tests/test_coverage_review.py
git commit -m "$(cat <<'EOF'
feat: add pipeline steps for coverage matrix and QA review

EOF
)"
```

---

### Task 4: 模板与 Prompt

**Files:**
- Create: `templates/coverage-matrix.md`
- Create: `templates/qa-review.md`
- Create: `skills/qa-orchestrator/templates/coverage-matrix.md`（与根模板相同）
- Create: `skills/qa-orchestrator/templates/qa-review.md`（与根模板相同）
- Modify: `templates/test-plan.md`
- Modify: `skills/qa-orchestrator/templates/test-plan.md`（同样插入 5.1）
- Modify: `qagent/agent/prompts.py`
- Modify: `qagent/agent/llm.py`

**Interfaces:**
- Consumes: `config.templates_dir`
- Produces:
  - `build_coverage_matrix_prompt(test_requirements_text: str, test_plan_text: str, risk_text: str, config: QAgentConfig) -> tuple[str, str]`
  - `build_fix_matrix_prompt(matrix_text: str, errors: list[str], test_plan_text: str, config: QAgentConfig) -> tuple[str, str]`
  - `build_qa_review_prompt(matrix_text: str, testcases_text: str, test_plan_text: str, risk_text: str, config: QAgentConfig) -> tuple[str, str]`
  - `build_test_plan_prompt` 的 user 文本含 `### 5.1 测试层级`
  - `build_testcases_prompt(..., coverage_matrix_text: str, ...)` 新增第 4 个文本参数（放在 `risk_text` 之后、`config` 之前）
  - `build_fix_prompt` 增加可选参数 `coverage_matrix_text: str = ""`、`review_text: str = ""`
  - user prompt 必须含字面量 `生成完整的 coverage-matrix.md` 与 `生成完整的 qa-review.md`
  - `MockLLM.task_markers` 增加这两条，放在 testcases 之前以免误匹配

- [ ] **Step 1: 写模板**

`templates/coverage-matrix.md`：

```markdown
# 覆盖矩阵：{功能名称}

> 由 QAgent 在生成用例之前输出，作为覆盖契约。`{...}` 为占位符。
> 校验脚本只解析「## 1. 覆盖契约」后的第一张表，列名不可改。

## 1. 覆盖契约

| 场景ID | 需求 | 场景 | 类别 | 优先级 | 判定方式 |
|--------|------|------|------|--------|----------|
| SC-001 | R1 | {一条可执行场景} | Happy | P0 | {可观察结果} |

## 2. 覆盖规则自检

- [ ] 每个 R 至少 1 行
- [ ] 有边界的 R 含 Boundary 或 Negative
- [ ] 每个 API 含 Happy + Negative（无 API 则写无）
- [ ] 无重复场景

## 3. 测试层级建议

| 层级 | 是否覆盖 | 覆盖目标 | 对应矩阵类别 |
|------|---------|---------|-------------|
| API | 是/否 | {目标} | Happy/Negative |
| UI/E2E | 是/否 | {目标} | Happy/State |
| 安全 | 是/否 | {目标} | Security |
| 性能 | 否 | 本期不测 | — |
```

`templates/qa-review.md`：

```markdown
# QA Review：{功能名称}

> 校验脚本只解析「## 1. 追溯表」后的第一张表，列名不可改。
> 结论枚举：COVERED / GAP / DUPLICATE / WEAK。GAP 的对应用例写 —。

## 1. 追溯表（SC ↔ TC）

| 场景ID | 对应用例 | 结论 |
|--------|----------|------|
| SC-001 | TC-XXX-001 | COVERED |

## 2. Coverage Gap

| 缺口 | 关联 | 严重度 | 建议 |
|------|------|--------|------|
| {无则写无} | | | |

## 3. Test Smell

| 用例 | Smell | 说明 |
|------|-------|------|
| {TC 或无} | 模糊预期/一条多断言/编造 API/重复场景/缺测试数据/不可判定 | {说明} |

## 4. 评审摘要

- 矩阵行数：{N}
- COVERED：{N}；GAP：{N}；WEAK：{N}
```

根目录与 `skills/qa-orchestrator/templates/` 各放一份，内容相同。

在两份 `test-plan.md` 的「## 5. 测试类型与策略」表格之后、「## 6.」之前插入：

```markdown

### 5.1 测试层级

| 层级 | 是否覆盖 | 覆盖目标 | 对应矩阵类别 |
|------|---------|---------|-------------|
| API | 是/否 | {接口/契约} | Happy/Negative |
| UI/E2E | 是/否 | {页面主路径} | Happy/State |
| 安全 | 是/否 | {权限/越权} | Security |
| 性能 | 否 | 本期不测 | — |
```

不要改 `testcase.schema.yaml` 的 `plan_required_sections`。

- [ ] **Step 2: 写 prompt 冒烟测试（失败）**

追加到 `tests/test_coverage_review.py`：

```python
from qagent.agent.prompts import (
    SYSTEM,
    build_coverage_matrix_prompt,
    build_fix_matrix_prompt,
    build_fix_prompt,
    build_qa_review_prompt,
    build_test_plan_prompt,
    build_testcases_prompt,
)
from qagent.config import resolve_config


def test_prompt_markers_and_signatures():
    cfg = resolve_config(workspace=REPO)
    sys_m, user_m = build_coverage_matrix_prompt("treq", "plan", "risk", cfg)
    assert "生成完整的 coverage-matrix.md" in user_m
    assert "覆盖契约" in user_m
    _, user_fix_m = build_fix_matrix_prompt("matrix", ["SC-001 类别非法"], "plan", cfg)
    assert "coverage-matrix.md" in user_fix_m
    _, user_r = build_qa_review_prompt("matrix", "cases", "plan", "risk", cfg)
    assert "生成完整的 qa-review.md" in user_r
    _, user_plan = build_test_plan_prompt("treq", "src", cfg)
    assert "### 5.1 测试层级" in user_plan
    _, user_tc = build_testcases_prompt("treq", "plan", "risk", "matrix", cfg)
    assert "矩阵" in user_tc
    _, user_fix = build_fix_prompt(
        "cases", ["GAP SC-002"], "plan", cfg,
        test_requirements_text="treq",
        coverage_matrix_text="matrix",
        review_text="review",
    )
    assert "SC-002" in user_fix or "GAP" in user_fix
    assert "矩阵" in SYSTEM or "coverage" in SYSTEM.lower() or "覆盖矩阵" in SYSTEM
```

- [ ] **Step 3: 跑测试确认失败**

Run: `pytest tests/test_coverage_review.py::test_prompt_markers_and_signatures -v`

Expected: FAIL，函数不存在或缺少参数

- [ ] **Step 4: 实现 prompts 与 Mock 标记**

`SYSTEM` 改为：

```python
SYSTEM = """你是 QAgent，资深 QA 测试设计专家 Agent。

标准流水线（严格按序，不可跳步）：
1. 从 PRD + 研发设计文档 → 生成详细 **测试需求**（覆盖清单，防漏测）
2. 从测试需求 → 生成 **测试方案**（R 编号需求条目 + 策略 + 测试层级）
3. 从方案 + 风险 → 生成 **覆盖矩阵**（SC 行，先于用例）
4. 从覆盖矩阵 → 生成 **测试用例**
5. 从用例回填 **QA Review**（SC↔TC、Gap、Smell）

输出纪律：
- 只输出目标 Markdown 文件正文，不要用 ```markdown 包裹全文
- 不要输出解释性前后缀
- 语言与需求文档一致
- 严格遵守模板结构与 Schema 契约

质量原则：
- 测试需求阶段尽可能穷举可测点，宁可清单长，不可漏模块/接口/边界
- 没有覆盖矩阵禁止写用例；用例必须能追溯到 SC 行
- 需求条目 R 必须可验证、可追溯到用例
- 拒绝模糊预期；步骤含具体数据；API 含 Method/Path/状态码"""
```

`build_test_plan_prompt` 的硬性要求增加一条：`6. 必须包含小节 ### 5.1 测试层级（API / UI-E2E / 安全 / 性能）`

新增（user 文本必须含指定字面量）：

```python
def build_coverage_matrix_prompt(
    test_requirements_text: str,
    test_plan_text: str,
    risk_text: str,
    config: QAgentConfig,
) -> tuple[str, str]:
    template = _read(config.templates_dir / "coverage-matrix.md")
    user = f"""请生成完整的 coverage-matrix.md（覆盖契约，先于用例）。

类别仅允许：Happy / Boundary / Negative / Security / State / Concurrency。
场景ID 格式 SC-001 起连续。每个 R 至少 1 行。判定方式必须可观察。
不要编造 Accessibility。不要输出用例 YAML。

--- test-requirements.md ---
{test_requirements_text}

--- test-plan.md ---
{test_plan_text}

--- risk.md ---
{risk_text}

模板：
{template}

只输出 coverage-matrix.md 正文。
"""
    return SYSTEM, user


def build_fix_matrix_prompt(
    matrix_text: str,
    errors: list[str],
    test_plan_text: str,
    config: QAgentConfig,
) -> tuple[str, str]:
    template = _read(config.templates_dir / "coverage-matrix.md")
    user = f"""coverage-matrix.md 校验失败，请修正后输出完整的 coverage-matrix.md。

--- 当前 coverage-matrix.md ---
{matrix_text}

--- test-plan.md ---
{test_plan_text}

--- 校验错误 ---
{chr(10).join(f"- {e}" for e in errors)}

模板：
{template}

只输出修正后的全文。不要删减合法 SC 来规避错误，应补行或改非法字段。
"""
    return SYSTEM, user


def build_qa_review_prompt(
    matrix_text: str,
    testcases_text: str,
    test_plan_text: str,
    risk_text: str,
    config: QAgentConfig,
) -> tuple[str, str]:
    template = _read(config.templates_dir / "qa-review.md")
    user = f"""请生成完整的 qa-review.md。

追溯表必须包含矩阵中每一个 SC。结论仅 COVERED / GAP / DUPLICATE / WEAK。
COVERED/DUPLICATE/WEAK 的对应用例必须是 testcases.md 中真实 id；GAP 写 —。

--- coverage-matrix.md ---
{matrix_text}

--- testcases.md ---
{testcases_text}

--- test-plan.md ---
{test_plan_text}

--- risk.md ---
{risk_text}

模板：
{template}

只输出 qa-review.md 正文。
"""
    return SYSTEM, user
```

改 `build_testcases_prompt` 签名为：

```python
def build_testcases_prompt(
    test_requirements_text: str,
    test_plan_text: str,
    risk_text: str,
    coverage_matrix_text: str,
    config: QAgentConfig,
) -> tuple[str, str]:
```

在 user 中插入矩阵全文，并加硬性要求：`矩阵每一行至少 1 条用例；禁止无矩阵行的用例；expected 必须能对应行内判定方式`。

`build_fix_prompt` 增加：

```python
    coverage_matrix_text: str = "",
    review_text: str = "",
```

若二者非空，追加到 user：

```python
    extra = ""
    if coverage_matrix_text:
        extra += f"\n--- coverage-matrix.md ---\n{coverage_matrix_text}\n"
    if review_text:
        extra += f"\n--- qa-review.md ---\n{review_text}\n"
```

`qagent/agent/llm.py` 的 `task_markers` 改为（矩阵/Review 放前面，避免被更短关键词抢走）：

```python
        task_markers = [
            ("生成完整的 coverage-matrix.md", "coverage-matrix"),
            ("生成完整的 qa-review.md", "qa-review"),
            ("生成完整的 testcases.md", "testcases"),
            ("生成完整的 risk.md", "risk.md"),
            ("生成完整的 test-plan.md", "test-plan"),
            ("生成完整的 test-requirements.md", "test-requirements"),
        ]
```

修矩阵走现有 `"修正" in user or "校验失败" in user` → `__fix__`。**矩阵修正与用例修正会共用 `__fix__`**。因此 runner 在矩阵阶段必须把 `__fix__` 设成矩阵内容，或让 `build_fix_matrix_prompt` **不要**含「校验失败」且 Mock 用别的键。

为避免冲突：`build_fix_matrix_prompt` 使用「矩阵结构无效，请修正」而不是「校验失败」。`MockLLM` 增加：

```python
        if "矩阵结构无效" in user:
            return self._responses.get("__fix_matrix__", self._responses.get("coverage-matrix", ""))
```

放在 `__fix__` 判断之前。

同步改 `build_fix_matrix_prompt` 的首句为：`coverage-matrix.md 矩阵结构无效，请修正后输出完整的 coverage-matrix.md。`

- [ ] **Step 5: 跑测试确认通过**

Run: `pytest tests/test_coverage_review.py::test_prompt_markers_and_signatures -v`

Expected: PASS。`templates_dir` 必须能读到新模板；`resolve_config` 的 `templates_dir` 来自 schema 父目录，即仓库 `templates/`。

- [ ] **Step 6: Commit**

```bash
git add templates/coverage-matrix.md templates/qa-review.md templates/test-plan.md \
  skills/qa-orchestrator/templates/coverage-matrix.md \
  skills/qa-orchestrator/templates/qa-review.md \
  skills/qa-orchestrator/templates/test-plan.md \
  qagent/agent/prompts.py qagent/agent/llm.py tests/test_coverage_review.py
git commit -m "$(cat <<'EOF'
feat: add coverage matrix and QA review prompts

EOF
)"
```

---

### Task 5: Runner 接入并更新 Agent 集成测试

**Files:**
- Modify: `qagent/agent/runner.py`
- Modify: `tests/test_agent.py`

**Interfaces:**
- Consumes: Task 4 全部 prompt 函数；`parse_coverage_matrix` / `parse_review_trace` / `validate_matrix` / `validate_review_trace`；`PipelineStep.COVERAGE_MATRIX` / `QA_REVIEW`
- Produces: `QAgentRunner.run` 按 spec 第 4/6 节执行；`RunResult.artifacts` 含 `coverage_matrix`、`qa_review`；`steps_completed` 含 `coverage_matrix`、`qa_review`

Runner 行为（按此实现，不要合并步骤）：

1. 现有 Step 2–4 不变（test-requirements / test-plan / risk）。`total_steps = 9`。日志用 `Step k/9`。
2. **矩阵：** 调用 `build_coverage_matrix_prompt` → 写文件 → `parse_coverage_matrix` + `parse_requirement_ids` + `validate_matrix`。有 error 则 `build_fix_matrix_prompt` 循环，最多 `retry_limit` 次（含首次生成）。仍失败：`result.errors = errors`，`success=False`，**return，不写用例**。成功则 `mark_step(..., COVERAGE_MATRIX)`。
3. **用例：** `build_testcases_prompt(..., coverage_matrix_text=matrix_content, config)`。
4. **Review：** `build_qa_review_prompt` → 写 `qa-review.md` → `mark_step(QA_REVIEW)`。
5. **校验循环：** `_full_validate` 在现有逻辑后追加：若矩阵/Review 文件缺失 → error；否则 `validate_matrix` + `validate_review_trace`（`case_ids` 来自已解析 cases 的 `id`）。失败则只 `build_fix_prompt(..., coverage_matrix_text=..., review_text=...)` 改用例，然后**再调用** `build_qa_review_prompt` 整份覆盖 `qa-review.md`。不回写矩阵。
6. 导出后 `artifacts` 增加两个路径。

`_full_validate` 追加示例：

```python
        from qagent.parsing import parse_coverage_matrix, parse_review_trace
        from qagent.validation import validate_matrix, validate_review_trace

        if not self.config.coverage_matrix_path.is_file():
            errors.append(f"缺少文件: {self.config.coverage_matrix_path}")
        if not self.config.qa_review_path.is_file():
            errors.append(f"缺少文件: {self.config.qa_review_path}")
        if self.config.coverage_matrix_path.is_file() and self.config.qa_review_path.is_file() and plan_path.is_file():
            try:
                matrix_rows = parse_coverage_matrix(self.config.coverage_matrix_path)
                review_rows = parse_review_trace(self.config.qa_review_path)
                req_ids = parse_requirement_ids(plan_path)
                m_err, m_warn = validate_matrix(matrix_rows, req_ids, self.config)
                errors.extend(m_err)
                warnings.extend(m_warn)
                case_ids = {str(c.get("id")) for c in cases if c.get("id")}
                r_err, r_warn = validate_review_trace(
                    review_rows,
                    {row.scenario_id for row in matrix_rows},
                    case_ids,
                    self.config,
                )
                errors.extend(r_err)
                warnings.extend(r_warn)
            except ValueError as exc:
                errors.append(str(exc))
```

注意：现有 `_full_validate` 在 `parse_cases` 之后才有 `cases`。矩阵/Review 校验必须放在 `cases` 解析成功之后；若 `testcases` 不存在，前面已经会因 parse 抛错——保持 `parse_cases` 在文件存在时调用。

- [ ] **Step 1: 先改 `tests/test_agent.py` 让它失败**

`mock_responses` 增加：

```python
        "coverage-matrix": (FIXTURES / "coverage-matrix.md").read_text(encoding="utf-8"),
        "qa-review": (FIXTURES / "qa-review.md").read_text(encoding="utf-8"),
        "__fix_matrix__": (FIXTURES / "coverage-matrix.md").read_text(encoding="utf-8"),
```

断言增加：

```python
    assert (out / "coverage-matrix.md").is_file()
    assert (out / "qa-review.md").is_file()
    assert "coverage_matrix" in result.artifacts
    assert "qa_review" in result.artifacts
    assert len(llm.calls) >= 6
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_agent.py::test_agent_run_mock -v`

Expected: FAIL（缺少产物或 `build_testcases_prompt` 参数不对）

- [ ] **Step 3: 改 runner**

按本任务「Runner 行为」改 `qagent/agent/runner.py`。矩阵阶段日志：`Step 5/9 生成 coverage-matrix.md`；用例 `Step 6/9`；Review `Step 7/9`；校验 `Step 8/9`；导出 `Step 9/9`。原 Step 2–4 编号保持 2–4。

矩阵循环伪代码（必须实现，不要只写注释）：

```python
        from qagent.agent.prompts import (
            build_coverage_matrix_prompt,
            build_fix_matrix_prompt,
            build_qa_review_prompt,
        )
        from qagent.parsing import parse_coverage_matrix, parse_requirement_ids
        from qagent.validation import validate_matrix

        self._log("Step 5/9 生成 coverage-matrix.md ...")
        sys_prompt, user_prompt = build_coverage_matrix_prompt(
            treq_content, plan_content, risk_content, self.config,
        )
        matrix_content = extract_document(self.llm.complete(sys_prompt, user_prompt))
        self._write(self.config.coverage_matrix_path, matrix_content)
        req_ids = parse_requirement_ids(self.config.test_plan_path)
        for attempt in range(1, self.config.retry_limit + 1):
            try:
                m_rows = parse_coverage_matrix(self.config.coverage_matrix_path)
                m_err, m_warn = validate_matrix(m_rows, req_ids, self.config)
            except ValueError as exc:
                m_err, m_warn = [str(exc)], []
            for w in m_warn:
                self._log(f"WARNING: {w}")
            if not m_err:
                break
            if attempt >= self.config.retry_limit:
                result.errors = m_err
                return result
            sys_prompt, user_prompt = build_fix_matrix_prompt(
                matrix_content, m_err, plan_content, self.config,
            )
            matrix_content = extract_document(self.llm.complete(sys_prompt, user_prompt))
            self._write(self.config.coverage_matrix_path, matrix_content)
        mark_step(self.config, PipelineStep.COVERAGE_MATRIX)
        result.steps_completed.append("coverage_matrix")
```

用例 prompt 改为传入 `matrix_content`。Review 写完后 `mark_step(QA_REVIEW)`。校验失败修正后立刻重出 Review（同一 attempt 内，先写 cases 再 review 再进入下一轮 `_full_validate`）。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_agent.py tests/test_coverage_review.py tests/test_qa_common.py -v`

Expected: 全部 PASS。`qagent.yaml` 默认 `strict_coverage: true`，但 `test_agent` 里手建的 `QAgentConfig` 未设 `strict_coverage`，默认 `False`；fixture 的 Review 全 COVERED，矩阵覆盖 R1–R3，应通过。

- [ ] **Step 5: Commit**

```bash
git add qagent/agent/runner.py tests/test_agent.py
git commit -m "$(cat <<'EOF'
feat: run coverage matrix and QA review in the agent pipeline

EOF
)"
```

---

### Task 6: CLI check / mock 接入

**Files:**
- Modify: `qagent/cli.py`

**Interfaces:**
- Consumes: `validate_matrix`、`validate_review_trace`、config 新路径
- Produces: `qagent check` 缺矩阵或 Review 则失败；`_run_validate` 追加同样校验；`qagent run --mock` 的 Mock 字典含 `coverage-matrix`、`qa-review`、`__fix_matrix__` 与 `test-requirements`

- [ ] **Step 1: 写 CLI 单测（失败）**

追加到 `tests/test_coverage_review.py`：

```python
from qagent.cli import main


def test_check_fails_without_matrix(tmp_path, capsys):
    out = tmp_path / "out"
    out.mkdir()
    (out / "test-plan.md").write_text(
        (FIXTURES / "test-plan.md").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (out / "risk.md").write_text(
        (FIXTURES / "risk.md").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (out / "testcases.md").write_text(
        (FIXTURES / "testcases-valid.md").read_text(encoding="utf-8"), encoding="utf-8"
    )
    rc = main(["check", "--out", str(out)])
    assert rc == 1
    assert "coverage-matrix" in capsys.readouterr().out
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_coverage_review.py::test_check_fails_without_matrix -v`

Expected: FAIL（`check` 仍只要求 plan/risk/cases，返回 0）或断言文案不匹配

- [ ] **Step 3: 改 CLI**

`cmd_check` 的清单改为：

```python
    for label, path in [
        ("test-plan", config.test_plan_path),
        ("risk", config.risk_path),
        ("coverage-matrix", config.coverage_matrix_path),
        ("testcases", config.testcases_path),
        ("qa-review", config.qa_review_path),
    ]:
```

`_run_validate` 在现有风险校验之后追加（文件不存在则 error）：

```python
    from qagent.parsing import parse_coverage_matrix, parse_review_trace
    from qagent.validation import validate_matrix, validate_review_trace

    matrix_path = config.coverage_matrix_path
    review_path = config.qa_review_path
    if not matrix_path.is_file():
        errors.append(f"缺少文件: {matrix_path}")
    if not review_path.is_file():
        errors.append(f"缺少文件: {review_path}")
    if matrix_path.is_file() and review_path.is_file() and plan_path.is_file():
        try:
            matrix_rows = parse_coverage_matrix(matrix_path)
            review_rows = parse_review_trace(review_path)
            m_err, m_warn = validate_matrix(matrix_rows, requirement_ids, config)
            errors.extend(m_err)
            warnings.extend(m_warn)
            case_ids = {str(c.get("id")) for c in cases if c.get("id")}
            r_err, r_warn = validate_review_trace(
                review_rows,
                {row.scenario_id for row in matrix_rows},
                case_ids,
                config,
            )
            errors.extend(r_err)
            warnings.extend(r_warn)
        except ValueError as exc:
            errors.append(str(exc))
```

`cmd_run` 的 Mock 字典改为：

```python
        fixtures = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
        llm = MockLLM({
            "test-requirements": (fixtures / "test-requirements-generated.md").read_text(encoding="utf-8"),
            "test-plan": (fixtures / "test-plan.md").read_text(encoding="utf-8"),
            "risk.md": (fixtures / "risk.md").read_text(encoding="utf-8"),
            "coverage-matrix": (fixtures / "coverage-matrix.md").read_text(encoding="utf-8"),
            "qa-review": (fixtures / "qa-review.md").read_text(encoding="utf-8"),
            "testcases": (fixtures / "testcases-valid.md").read_text(encoding="utf-8"),
            "__fix__": (fixtures / "testcases-valid.md").read_text(encoding="utf-8"),
            "__fix_matrix__": (fixtures / "coverage-matrix.md").read_text(encoding="utf-8"),
        })
```

`cmd_generate` 的提示文案若仍写「Step 1-4」，改为「按 qa-orchestrator 完成生成，再 qagent check」。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_coverage_review.py tests/test_agent.py tests/test_qa_common.py -v`

Expected: PASS

再用仓库根目录跑一次 mock（需要已 `pip install -e .`）：

Run: `qagent run input/requirement-example.md --out /tmp/qagent-mock-out --mock`

Expected: 退出码 0；该目录含 `coverage-matrix.md`、`qa-review.md`、`testcases.xlsx`

- [ ] **Step 5: Commit**

```bash
git add qagent/cli.py tests/test_coverage_review.py
git commit -m "$(cat <<'EOF'
feat: validate coverage matrix and review in qagent check

EOF
)"
```

---

### Task 7: 文档与 Skill 对齐

**Files:**
- Modify: `AGENT.md`
- Modify: `README.md`
- Modify: `skills/qa-orchestrator/SKILL.md`
- Modify: `skills/qa-test-design/SKILL.md`
- Modify: `skills/qa-testcase-generator/SKILL.md`
- Modify: `docs/superpowers/specs/2026-08-18-qa-coverage-review-design.md`（状态改为「已实现」仅在本任务全部文档改完且 pytest 绿之后）

**Interfaces:**
- Consumes: 已实现流水线
- Produces: 文档与代码步骤一致；不新增 Skill 目录

- [ ] **Step 1: 改 AGENT.md 架构图与能力列表**

能力输出改为含 `coverage-matrix.md`、`qa-review.md`。

架构改为：

```text
需求文档（PRD + 设计）
   ↓ Step 2  test-requirements.md
   ↓ Step 3  test-plan.md
   ↓ Step 4  risk.md
   ↓ Step 5  coverage-matrix.md
   ↓ Step 6  testcases.md
   ↓ Step 7  qa-review.md
[QAgentRunner]
   ↓ Step 8  validate
   ↓ Step 9  export xlsx
```

- [ ] **Step 2: 改 README.md**

「设计原则 / 使用 / Agent 内部流程」加上矩阵与 Review。二期规划仍保留 Playwright，不要写成已实现。

- [ ] **Step 3: 改三条 Skill**

`qa-orchestrator/SKILL.md` 步骤表插入 Step 5 矩阵、Step 7 Review，校验改为 Step 8，导出 Step 9。完成标准列出 7 个产物。反模式增加：「跳过覆盖矩阵直接写用例」。

`qa-test-design/SKILL.md`：在风险分析之后增加「第四部分：覆盖矩阵」——先写 `coverage-matrix.md` 再写用例；并说明 test-plan 必须有 `### 5.1 测试层级`。description 触发词加上 coverage matrix / 覆盖矩阵。

`qa-testcase-generator/SKILL.md`：输入增加 `coverage-matrix.md`；覆盖规则第 1 条改为「矩阵每一行至少 1 条」；文末要求生成后必须写 `qa-review.md`。description 加上 qa-review / 覆盖矩阵。

- [ ] **Step 4: 全量验证**

Run: `pytest -v`

Expected: 全部 PASS

Run: `qagent run input/requirement-example.md --out /tmp/qagent-mock-out --mock && ls /tmp/qagent-mock-out`

Expected: 七个产物都在。

确认 `git diff templates/testcase.schema.yaml` 为空（若该文件本就未提交，则确认本次改动未编辑它）。

- [ ] **Step 5: 把 spec 状态改为已实现并 Commit**

```bash
git add AGENT.md README.md \
  skills/qa-orchestrator/SKILL.md \
  skills/qa-test-design/SKILL.md \
  skills/qa-testcase-generator/SKILL.md \
  docs/superpowers/specs/2026-08-18-qa-coverage-review-design.md
git commit -m "$(cat <<'EOF'
docs: document coverage matrix and QA review pipeline

EOF
)"
```

---

## Spec coverage（自检）

| Spec 条目 | 对应任务 |
|-----------|----------|
| 流水线插入矩阵 / Review | Task 3, 5 |
| 矩阵表契约与解析范围 | Task 1, 4 |
| Review 追溯表契约 | Task 1, 4 |
| 不改 Schema | Global + Task 7 验证 |
| test-plan 5.1、不改 plan_required_sections | Task 4 |
| prompt 函数与 Mock 短语 | Task 4 |
| 矩阵失败只修矩阵 | Task 5 |
| 用例失败重出 Review、不改矩阵 | Task 5 |
| validate_matrix / validate_review_trace 规则 | Task 2 |
| check / mock CLI | Task 6 |
| Skill / README / AGENT | Task 7 |
| 不做自动化 / 不新建 Skill 包 | Global + Task 7 |
| `qagent run --mock` 七产物 | Task 6 Step 4、Task 7 Step 4 |
