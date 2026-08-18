"""Agent 提示词构建。"""

from __future__ import annotations

import re
from pathlib import Path

from qagent.config import QAgentConfig
from qagent.schema import TestcaseSchema, load_schema

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


def extract_document(text: str) -> str:
    """从 LLM 响应中提取 Markdown 正文。"""
    text = text.strip()
    fenced = re.match(r"^```(?:markdown|md)?\s*\n(.*)\n```\s*$", text, re.DOTALL)
    if fenced:
        return fenced.group(1).strip()
    return text


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _schema_summary(schema: TestcaseSchema) -> str:
    lines = ["用例 YAML 字段契约："]
    for name, spec in schema.fields.items():
        req = "必填" if spec.required else "可选"
        extra = ""
        if spec.enum:
            extra = f"，枚举: {sorted(spec.enum)}"
        if spec.pattern:
            extra += f"，格式: {spec.pattern.pattern}"
        lines.append(f"- {name} ({req}{extra})")
    return "\n".join(lines)


def _user_supplement_hint(source_text: str) -> str:
    if "## 测试需求" not in source_text:
        return ""
    return """
【用户补充】源文档中含用户提供的测试需求章节，生成时合并采纳，冲突处以 PRD/设计文档为准并在第 10 节标注。
"""


TEST_REQUIREMENTS_GUIDE = """
## 测试需求生成要求（防漏测）

你必须完整阅读 PRD **和** 研发设计文档，输出可执行的测试需求（不是测试方案、不是用例）。

1. **穷举模块**：每个功能模块、API、页面流程都要有测试要点，禁止只写主流程
2. **API 清单**：从设计文档提取每个 endpoint 的 Method/Path，列出必测场景（成功/参数错误/权限/404 等）
3. **边界清单**：所有数值/大小/次数/超时/像素/长度约束，列出具体边界点与测试数据
4. **异常清单**：非法格式、服务失败、重复操作、并发、越权
5. **非功能**：性能 SLA、兼容性、权限、安全（设计文档有则必列）
6. **覆盖矩阵**：模块 ×（功能/接口/边界/异常/安全/性能），必测打 ✓
7. **追溯预备清单**：为后续 R 编号准备 PRE-x 条目，确保 PRD 每条规则、设计文档每个 API 都可追溯
8. **不测项**：明确写出不在范围的内容，避免过度测试
9. **待确认项**：文档矛盾或缺失处列出，不要静默假设
"""


def build_test_requirements_prompt(
    source_text: str,
    source_path: Path,
    config: QAgentConfig,
) -> tuple[str, str]:
    template = _read(config.templates_dir / "test-requirements-output.md")
    supplement = _user_supplement_hint(source_text)
    user = f"""请根据 PRD 与研发设计文档，生成完整的 test-requirements.md（详细测试需求，用于驱动后续 test-plan 与用例，目标是**尽量不漏测**）。

源文档：{source_path.name}
{supplement}

{TEST_REQUIREMENTS_GUIDE}

--- PRD / 设计文档合并正文 ---
{source_text}
--- 正文结束 ---

输出模板（替换 {{...}}，保留全部 10 个章节）：
{template}

硬性要求：
1. 第 3~5 节表格行数：简单功能 ≥8 行，复杂系统（OCR/多 API）≥25 行
2. 第 4 节 API 清单必须来自设计文档，不得编造 Path
3. 第 8 节覆盖矩阵每个必测模块至少一行
4. 第 9 节 PRE 条目应覆盖 PRD 所有业务规则
5. 只输出 test-requirements.md 正文
"""
    return SYSTEM, user


def build_test_plan_prompt(
    test_requirements_text: str,
    source_text: str,
    config: QAgentConfig,
) -> tuple[str, str]:
    template = _read(config.templates_dir / "test-plan.md")
    user = f"""请基于**已评审的测试需求**生成完整的 test-plan.md（测试方案）。

【输入优先级】test-requirements.md > PRD/设计文档摘要。测试需求中的必测项必须在方案中体现。

{ANALYSIS_GUIDE}

--- test-requirements.md（主要依据）---
{test_requirements_text}

--- PRD/设计文档摘要（补充细节，前 8000 字）---
{source_text[:8000]}

模板（替换 {{...}}，保留全部章节）：
{template}

硬性要求：
1. `## 2. 需求条目清单` 的 ```requirements 块：R 编号与第 9 节 PRE 条目一一对应或细化，不得遗漏 PRE
2. 每条 R 描述可验证（条件+行为+判定结果）；API 类 R 含 Method/Path
3. `## 4. 测试范围` 与测试需求第 2 节一致
4. `## 6. 测试设计技术选择` 覆盖测试需求第 3~6 节各模块
5. 只输出 test-plan.md 正文
6. 必须包含小节 ### 5.1 测试层级（API / UI-E2E / 安全 / 性能）
"""
    return SYSTEM, user


ANALYSIS_GUIDE = """
## 需求分析深度要求（test-plan 生成时必须完成）

1. **结构化拆解**
   - 功能需求：用户可见行为、业务规则、状态流转
   - 接口需求：REST/HTTP Method、Path、请求/响应字段、状态码语义
   - 非功能：性能 SLA、兼容性、权限、安全、并发
   - 数据规则：格式、长度、范围、枚举、唯一性、默认值

2. **需求条目 R 编号规则**
   - 每条 R 必须独立可验证，描述包含：条件 + 行为 + 可判定结果
   -  bad: "支持上传图片"  good: "上传 JPG/PNG 且 ≤4MB 时返回 200 并进入识别队列"
   - 接口类需求单独编号（如 R-API-xxx 或集中在 R 序号中明确 API 行为）
   - 从 PRD **和** 设计文档中提取，设计文档中的字段/错误码/路由规则不可遗漏

3. **测试设计技术选型（第 6 节）**
   - 每个输入域：等价类 + 边界值（min, min+1, max-1, max, 非法值）
   - 计数/配额/超时：n-1, n, n+1 三点
   - 多条件组合：判定表或 pairwise（注明选取策略）
   - 状态机：合法迁移 + 至少 1 条非法迁移
   - API：正常响应 + 参数缺失/类型错误/越权/404/409 等

4. **需求假设**
   - 文档未定义的错误文案、超时、重试、并发策略 → 列入假设，不得静默编造通过标准
"""


TESTCASE_QUALITY_GUIDE = """
## 用例质量硬性标准（违反则视为无效用例）

1. **原子性**：一条用例只验证一个核心点；禁止"并验证 A、B、C 均正确"

2. **可执行步骤**
   - UI：明确页面/按钮/输入框名称，每步一个动作
   - API：步骤中写出 `POST /api/v1/xxx`、Content-Type、关键 JSON 字段及具体值
   - 含**具体测试数据**（文件名、大小、像素、ID、错误码），禁止"输入合法/非法数据"

3. **可判定预期 expected**
   - 必须含至少一项：HTTP 状态码、业务 errorCode、界面精确文案、数据库/列表可见变化、文件内容结构
   - 禁止："成功"、"正常"、"符合预期"、"无报错"

4. **覆盖深度**（在测试需求或 PRD 范围内）
   - 每个 R **至少 1 条正向**；有边界定义的 R **必须有边界/异常用例**
   - CRITICAL 风险 → P0 且含正常+异常；HIGH → P0/P1
   - API 清单中每个 endpoint 至少：1 成功 + 1 典型失败（参数/权限）
   - 识别/路由/模板类：覆盖规则表中的关键组合，非只测 happy path

5. **模块与 ID**
   - 按功能模块分组；ID 格式 TC-<模块缩写>-<序号>，模块如 OCR/UP/TMPL/API/EXP
   - 复杂系统（需求条目 ≥15 或 多 PDF）：**30~80 条**；简单功能 15~40 条

6. **preconditions**
   - 写清登录态、权限、前置数据、服务 Mock 状态；无则 `[]`

7. **design_method**
   - 与用例实际手法一致（边界值/等价类/判定表/状态转换/场景法/错误推测/pairwise）
"""


def build_risk_prompt(
    test_requirements_text: str,
    test_plan_text: str,
    config: QAgentConfig,
) -> tuple[str, str]:
    template = _read(config.templates_dir / "risk.md")
    user = f"""请生成完整的 risk.md。

--- test-requirements.md ---
{test_requirements_text[:5000]}

--- test-plan.md ---
{test_plan_text}

模板：
{template}

分析要求：风险关联 R 编号；≥10 分需失效模式分析。
表格列名：编号 | 风险描述 | 影响度 | 可能性 | 风险分 | 分区 | 关联需求 | 对应用例优先级
"""
    return SYSTEM, user


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
    user = f"""coverage-matrix.md 矩阵结构无效，请修正后输出完整的 coverage-matrix.md。

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


def build_testcases_prompt(
    test_requirements_text: str,
    test_plan_text: str,
    risk_text: str,
    coverage_matrix_text: str,
    config: QAgentConfig,
) -> tuple[str, str]:
    schema = load_schema(config.schema_path)
    example = _read(config.templates_dir / "testcase.example.yaml")

    user = f"""请生成完整的 testcases.md。

【覆盖依据优先级】coverage-matrix.md > test-requirements.md 清单 > test-plan R 条目 > risk.md。
测试需求第 3~5 节每个要点至少 1 条用例；第 4 节每个 API 至少成功+失败各 1 条。

{TESTCASE_QUALITY_GUIDE}

--- test-requirements.md ---
{test_requirements_text}

--- test-plan.md ---
{test_plan_text}

--- risk.md ---
{risk_text}

--- coverage-matrix.md ---
{coverage_matrix_text}

{_schema_summary(schema)}

示例 YAML（**每个用例单独一个 ```yaml 块**，块内为单个 dict，不要用 `- id:` 列表）：
{example}

硬性要求：
1. 每个 R 至少 1 条用例；测试需求清单项不得遗漏
2. API/边界/异常清单必须有对应用例
3. 复杂系统 30~80 条；只输出 testcases.md 正文
4. 矩阵每一行至少 1 条用例；禁止无矩阵行的用例；expected 必须能对应行内判定方式
"""
    return SYSTEM, user


def build_fix_prompt(
    testcases_text: str,
    errors: list[str],
    test_plan_text: str,
    config: QAgentConfig,
    test_requirements_text: str = "",
    coverage_matrix_text: str = "",
    review_text: str = "",
) -> tuple[str, str]:
    schema = load_schema(config.schema_path)
    treq_block = f"\n--- test-requirements.md ---\n{test_requirements_text}\n" if test_requirements_text else ""
    extra = ""
    if coverage_matrix_text:
        extra += f"\n--- coverage-matrix.md ---\n{coverage_matrix_text}\n"
    if review_text:
        extra += f"\n--- qa-review.md ---\n{review_text}\n"
    user = f"""testcases.md 校验失败，请修正后输出**完整** testcases.md。

{TESTCASE_QUALITY_GUIDE}
{treq_block}{extra}
--- 当前 testcases.md ---
{testcases_text}

--- test-plan.md ---
{test_plan_text}

--- 校验错误 ---
{chr(10).join(f"- {e}" for e in errors)}

{_schema_summary(schema)}

只输出修正后的全文，补全遗漏 R 与测试需求要点，不要降低质量。
"""
    return SYSTEM, user
