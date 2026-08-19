# QAgent 独立小 Agent 清单
# 这是一个可脱离 Cursor/IDE 独立运行的 QA 文档生成 Agent

name: QAgent
version: 0.3.0
role: QA 测试方案与用例生成 Agent

## 能力

- 输入：PRD / 需求 Markdown（可含设计文档）
- 输出：test-requirements.md、test-plan.md、test-plan-mindmap.md、test-plan.mm、risk.md、coverage-matrix.md、testcases.md、qa-review.md、testcases.xlsx
- 方法：LLM 生成 + Schema / 矩阵 / Review 强校验 + 失败自动修正

## 运行方式

### 独立 Agent（推荐）

```bash
export OPENAI_API_KEY=sk-...
qagent run input/requirement-example.md --out output
```

### 内网服务（多人 + 对话修订）

```bash
export QAGENT_TOKEN=内网口令
qagent serve --host 0.0.0.0 --port 8765 --no-browser
```

浏览器打开服务地址：上传文档、整跑或只跑矩阵后、下载产物，并在右侧对话里局部修改方案/用例。飞书事件订阅 `POST /api/feishu/event`。部署见 `deploy/README.md`。

### 嵌入 Cursor（Skill 模式）

安装技能后对话触发：`/qa generate input/xxx.md`

### 离线测试

```bash
qagent run input/requirement-example.md --out output --mock
```

## 架构

```text
需求文档（PRD + 设计）
   ↓ Step 2  test-requirements.md
   ↓ Step 3  test-plan.md + 思维导图（md / FreeMind .mm）
   ↓ Step 4  risk.md
   ↓ Step 5  coverage-matrix.md
   ↓ Step 6  testcases.md
   ↓ Step 7  qa-review.md
[QAgentRunner]
   ↓ Step 8  validate
   ↓ Step 9  export xlsx
```

## 配置

见 `qagent.yaml` 中 `llm` 段：

```yaml
llm:
  model: gpt-4o-mini
  base_url: https://api.openai.com/v1
  api_key_env: OPENAI_API_KEY
  temperature: 0.2
```

## 与 Skill 的关系

| 模式 | 适用场景 |
|------|---------|
| `qagent run` | CI、命令行、无 IDE |
| `qagent serve` | 内网 Web / API / 飞书 |
| Cursor Skill | 对话式、人工介入 |
| `qagent check` | 仅校验已有产物 |

Skill 负责「人在回路」；独立 Agent 负责「一键全自动」。
