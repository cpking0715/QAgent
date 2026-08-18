# QAgent：独立 QA 测试 Agent

从需求文档出发，按固定流水线生成 **测试需求 → 测试方案 → 风险分析 → 覆盖矩阵 → 结构化测试用例 → QA Review（md/xlsx）**。

**两种运行形态：**

| 形态 | 入口 | 场景 |
|------|------|------|
| **独立 Agent** | `qagent run` | 命令行、CI、无 IDE，LLM 全自动 |
| **Cursor Skill** | `/qa generate` | 对话式、人工可介入 |

详见 [`AGENT.md`](AGENT.md)。

## 设计原则

- **确定性流水线**：orchestrator 按固定顺序执行；先写覆盖矩阵再写用例，用例后做 QA Review；Step 8–9 由 `qagent` CLI 强制校验与导出。
- **结构化契约**：用例 Schema 唯一事实来源为 `templates/testcase.schema.yaml`；矩阵与 Review 另有独立 Markdown 契约。
- **可追溯**：每条用例引用需求 R 编号；矩阵每一行至少 1 条用例；CRITICAL 风险需 P0 用例（脚本校验）。

## 目录结构

```
QAgent/
├── AGENT.md                     # 独立 Agent 清单
├── qagent/
│   ├── agent/                   # 独立小 Agent（LLM + 流水线）
│   │   ├── llm.py               # OpenAI 兼容 / Mock
│   │   ├── prompts.py           # 提示词
│   │   └── runner.py            # 9 步运行器
│   ├── cli.py
│   └── ...
```

## 安装

### 1. Python 包与 CLI

```bash
pip install -e .
qagent --help
```

### 2. Agent 技能

```bash
chmod +x scripts/install-skill.sh
./scripts/install-skill.sh .cursor/skills
```

| Agent 环境 | 项目级技能目录 |
|-----------|---------------|
| Cursor | `.cursor/skills/` |
| Qoder | `.qoder/skills/` |
| Claude Code | `.claude/skills/` |

技能安装后，`qa-orchestrator` 自带 `templates/` 与 `config.defaults.yaml`，不依赖仓库根目录结构。

### 3. 工作区配置（可选）

复制并修改 [`qagent.yaml`](qagent.yaml)：

```yaml
input_dir: input
output_dir: output
language: zh
schema: templates/testcase.schema.yaml
retry_limit: 3
strict_coverage: false

llm:                            # 独立 Agent（qagent run）配置
  model: gpt-4o-mini
  base_url: https://api.openai.com/v1
  api_key_env: OPENAI_API_KEY
```

**API Key（二选一）：**

```bash
cp qagent.local.yaml.example qagent.local.yaml
# 编辑 qagent.local.yaml → llm.api_key: "sk-..."
```

或 `export OPENAI_API_KEY=sk-...`（`qagent.local.yaml` 已在 `.gitignore`，不会提交）

## 使用

### 独立 Agent（上传文档 → 自动生成）

**推荐：Web 上传界面**

```bash
qagent serve
```

浏览器打开 http://127.0.0.1:8765 ，上传 PRD/需求文档（支持 md、txt、pdf、docx），点击「开始生成」。

**或命令行：**

```bash
# 将文档放到 input/uploads/ 后
qagent run --uploads

# 直接指定多个文档（可加 测试需求.md）
qagent run OCR-PRD.pdf OCR设计文档.pdf 测试需求.md --out output/ocr

# 离线测试
qagent run --uploads --mock
```

### 测试需求（强烈建议）

仅 PRD/设计文档时，用例容易偏粗。请补充 **测试需求**，明确测什么、不测什么、API/边界/环境重点：

```bash
cp templates/test-requirements.example.md input/test-requirements.md
```

或上传/附加名为 `测试需求.md` 的文件。Agent 会优先遵循该章节。

Agent 内部流程（防漏测）：

1. **测试需求**（test-requirements.md）：从 PRD + 设计文档穷举可测点、API/边界/异常清单
2. **测试方案**（test-plan.md）：基于测试需求生成 R 编号需求条目与策略（含 `### 5.1 测试层级`）
3. **风险分析**（risk.md）：5×5 风险矩阵
4. **覆盖矩阵**（coverage-matrix.md）：计划覆盖契约（先于用例，不可跳过）
5. **测试用例**（testcases.md）：矩阵每一行至少 1 条用例
6. **QA Review**（qa-review.md）：SC↔TC 追溯、Gap、Smell
7. **校验与导出**：脚本校验 → 失败则 LLM 修正 → 导出 xlsx

### Cursor Skill 对话

```
/qa generate input/requirement-example.md
```

或："根据 PRD 生成测试方案和测试用例"。

### CLI

```bash
# 初始化流水线
qagent generate input/requirement-example.md --out output

# 校验全部产物（test-requirements + plan + risk + matrix + cases + review）
qagent check --out output

# 分步
qagent validate output/testcases.md --plan output/test-plan.md --risk output/risk.md
qagent export output/testcases.md --out output/testcases.xlsx --plan output/test-plan.md

# Step 8-9 一步完成
qagent pipeline validate-export --out output
qagent pipeline status --out output
```

兼容旧脚本（等价于 qagent 子命令）：

```bash
python scripts/validate_cases.py output/testcases.md --plan output/test-plan.md
python scripts/export_xlsx.py output/testcases.md --out output/testcases.xlsx --plan output/test-plan.md
```

## Schema 契约

- 机器可读：[`templates/testcase.schema.yaml`](templates/testcase.schema.yaml)
- 人类示例：[`templates/testcase.example.yaml`](templates/testcase.example.yaml)
- 修改契约只需更新 schema 文件；技能文档与示例与之对齐

## 测试

```bash
pip install -e ".[dev]"
pytest
```

## 二期规划

- Playwright 自动化、Allure 报告
- Jira/Xray/TestRail 导出插件（见 `qagent.exporters`）
