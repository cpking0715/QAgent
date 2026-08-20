"""Agent 提示词构建。数值规则统一来自 templates/rules.yaml（qagent/rules.py），
枚举契约来自 templates/testcase.schema.yaml，本文件不再手写这些数值。"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from qagent.config import QAgentConfig
from qagent.parsing import filter_requirements_block
from qagent.rules import load_rules
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


@lru_cache(maxsize=64)
def _read(path: Path) -> str:
    # 模板文件运行期不变，缓存避免批次循环内反复读盘
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
【用户测试范围优先】源文档含用户提供的测试需求章节。
- 必测 / 不测 / 测试类型以用户为准，PRD 只用来填写清单细节。
- 用户写明「不测」的模块、以及未要求的类型（如性能、压力、兼容）不得写入第 3~6 节覆盖要点，也不得在第 8 节打 ✓。
- 被排除项可在第 10 节「需求假设」注明「按用户范围排除 xxx」。
- 不要用 PRD 里的 SLA/性能描述去覆盖用户的「不测性能」。
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
    rules = load_rules(config.rules_path)
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
1. {rules.checklist_rule()}
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

5. **模块与数量**
   - 按功能模块分组；ID 格式 TC-<模块缩写>-<序号>，模块如 OCR/UP/TMPL/API/EXP
   - {case_count_rule}

6. **preconditions**
   - 写清登录态、权限、前置数据、服务 Mock 状态；无则 `[]`

7. **design_method**
   - 与用例实际手法一致（边界值/等价类/判定表/状态转换/场景法/错误推测/pairwise）
"""


def _quality_guide(config: QAgentConfig) -> str:
    """数量规则从 templates/rules.yaml 渲染（唯一事实来源）。"""
    rules = load_rules(config.rules_path)
    return TESTCASE_QUALITY_GUIDE.replace("{case_count_rule}", rules.case_count_rule())


def _bounded(text: str, budget: int) -> str:
    text = text or ""
    if len(text) <= budget:
        return text
    return text[:budget] + "\n…（已按预算截断，完整内容见产物文件）"


def _batch_context(
    config: QAgentConfig,
    treq: str,
    plan: str,
    risk: str,
    requirement_ids: list[str] | None = None,
) -> tuple[str, str, str]:
    """批次 prompt 上下文裁剪。

    full（默认，与历史行为一致）：携带上游产物全文；
    sliced：plan 的 requirements 块只保留本批 R，三份文档按字符预算截断，
            降低批次数带来的 token 线性放大。
    """
    if config.prompt_context_mode != "sliced":
        return treq, plan, risk
    if requirement_ids:
        plan = filter_requirements_block(plan, requirement_ids)
    return (
        _bounded(treq, config.prompt_treq_budget),
        _bounded(plan, config.prompt_plan_budget),
        _bounded(risk, config.prompt_risk_budget),
    )


def build_risk_prompt(
    test_requirements_text: str,
    test_plan_text: str,
    config: QAgentConfig,
) -> tuple[str, str]:
    template = _read(config.templates_dir / "risk.md")
    high_min = int(
        load_schema(config.schema_path).risk_zones.get("HIGH", {}).get("min_score", 10)
    )
    user = f"""请生成完整的 risk.md。

--- test-requirements.md ---
{test_requirements_text[:5000]}

--- test-plan.md ---
{test_plan_text}

模板：
{template}

分析要求：风险关联 R 编号；≥{high_min} 分需失效模式分析。
表格列名：编号 | 风险描述 | 影响度 | 可能性 | 风险分 | 分区 | 关联需求 | 对应用例优先级
"""
    return SYSTEM, user


def build_coverage_matrix_prompt(
    test_requirements_text: str,
    test_plan_text: str,
    risk_text: str,
    config: QAgentConfig,
    requirement_ids: list[str] | None = None,
) -> tuple[str, str]:
    template = _read(config.templates_dir / "coverage-matrix.md")
    batch_hint = ""
    if requirement_ids:
        listed = ", ".join(requirement_ids)
        batch_hint = (
            f"\n本批只覆盖这些需求（每个至少 1 行）：{listed}\n"
            "不要写这些 R 以外的行。场景ID 可从 SC-001 起，脚本会重编号。\n"
        )
    treq_sliced, plan_sliced, risk_sliced = _batch_context(
        config, test_requirements_text, test_plan_text, risk_text, requirement_ids,
    )
    user = f"""请生成完整的 coverage-matrix.md（覆盖契约，先于用例）。
{batch_hint}
类别仅允许：Happy / Boundary / Negative / Security / State / Concurrency。
场景ID 格式 SC-001 起连续。每个 R 至少 1 行。判定方式必须可观察。
不要编造 Accessibility。不要输出用例 YAML。

--- test-requirements.md ---
{treq_sliced}

--- test-plan.md ---
{plan_sliced}

--- risk.md ---
{risk_sliced}

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
    requirement_ids: list[str] | None = None,
) -> tuple[str, str]:
    schema = load_schema(config.schema_path)
    example = _read(config.templates_dir / "testcase.example.yaml")
    treq_sliced, plan_sliced, risk_sliced = _batch_context(
        config, test_requirements_text, test_plan_text, risk_text, requirement_ids,
    )

    user = f"""请生成完整的 testcases.md。

【覆盖依据优先级】coverage-matrix.md > test-requirements.md 清单 > test-plan R 条目 > risk.md。
测试需求第 3~5 节每个要点至少 1 条用例；第 4 节每个 API 至少成功+失败各 1 条。

{_quality_guide(config)}

--- test-requirements.md ---
{treq_sliced}

--- test-plan.md ---
{plan_sliced}

--- risk.md ---
{risk_sliced}

--- coverage-matrix.md ---
{coverage_matrix_text}

{_schema_summary(schema)}

示例 YAML（**每个用例单独一个 ```yaml 块**，块内为单个 dict，不要用 `- id:` 列表）：
{example}

硬性要求：
1. 每个 R 至少 1 条用例；测试需求清单项不得遗漏
2. API/边界/异常清单必须有对应用例
3. 只输出 testcases.md 正文
4. 矩阵切片每一行恰好 1 条用例，总数必须等于切片行数；禁止多写、禁止切片外用例；expected 必须能对应行内判定方式
5. **禁止**用 `- id:` YAML 列表；每个用例单独一个已闭合的 ```yaml 块，块内是映射（以 `id:` 开头）
6. `requirement_ref` 只能填方案中的 R 编号（如 R1），禁止填 SC- / F / A / B / PRE
7. `title` 必须以场景ID开头，例如 `SC-001 未注册手机号正确注册`
8. `steps` / `expected` 里不要用反引号；JSON 或含 `{{` `}}` 的文本必须写成双引号字符串
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

{_quality_guide(config)}
{treq_block}{extra}
--- 当前 testcases.md ---
{testcases_text}

--- test-plan.md ---
{test_plan_text}

--- 校验错误 ---
{chr(10).join(f"- {e}" for e in errors)}

{_schema_summary(schema)}

硬性要求：
1. 每个用例单独一个已闭合的 ```yaml 映射块，禁止 `- id:` 列表
2. `requirement_ref` 只能是 R 编号
3. 优先补全缺失场景，不要删除已有正确用例
4. 只输出 testcases.md 正文
"""
    return SYSTEM, user
