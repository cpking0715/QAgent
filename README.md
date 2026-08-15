# QAgent：AI 测试方案与用例生成 Agent（一期）

从需求文档出发，按固定流水线生成 **测试方案 → 风险分析 → 结构化测试用例（md/xlsx）**。
方法论取材并改写自开源 QA Skills（softaworks/agent-toolkit、petrkindlmann/qa-skills、fugazi/test-automation-skills-agents），审查记录见 `reference/`（不纳入交付）。

## 设计原则

- **确定性流水线**：orchestrator 按固定顺序执行，不依赖模型自主路由，保证产物稳定。
- **结构化契约**：用例输出为 YAML 块，字段契约见 `templates/testcase.yaml`，由脚本强校验。
- **可追溯**：每条用例必须引用需求条目（R 编号），风险项映射用例优先级。

## 目录结构

```
QAgent/
├── skills/                      # 三个技能（需安装到 Agent 的 skills 目录，见下文）
│   ├── qa-orchestrator/         # 流水线入口（/qa generate）
│   ├── qa-test-design/          # 需求拆解 + 测试方案 + 风险矩阵
│   └── qa-testcase-generator/   # 方案/风险 → 结构化用例
├── templates/                   # 产物模板与 Schema 契约
│   ├── testcase.yaml            # 用例字段契约（唯一事实来源）
│   ├── test-plan.md
│   └── risk.md
├── scripts/
│   ├── qa_common.py             # 解析与校验公共逻辑
│   ├── validate_cases.py        # 用例校验（依赖 pyyaml）
│   └── export_xlsx.py           # 用例导出 Excel（依赖 pyyaml、openpyxl）
├── input/                       # 需求文档放这里
├── output/                      # 生成产物（gitignore）
└── reference/                   # 开源仓库审查取材（gitignore）
```

## 安装

技能放在本仓库 `skills/` 下。要让 Agent 自动发现，把三个技能目录复制到对应位置：

| Agent 环境 | 项目级技能目录 |
|-----------|---------------|
| Qoder | `.qoder/skills/` |
| Claude Code | `.claude/skills/` |

例如在 Qoder 项目根目录执行：

```powershell
Copy-Item -Recurse skills\* .qoder\skills\
```

Python 依赖（脚本运行需要）：

```powershell
pip install pyyaml openpyxl
```

## 使用

在 Agent 对话中：

```
/qa generate input/requirement-example.md
```

或自然语言："根据 input/xxx.md 生成测试方案和测试用例"。

流水线固定为 6 步：解析需求 → test-plan.md → risk.md → testcases.md → 校验 → 导出 xlsx。
校验失败时 Agent 会自动修正并重试（最多 3 次）。

### 产物说明

| 文件 | 内容 |
|------|------|
| `output/test-plan.md` | 需求条目清单（R1..Rn）、测试范围、策略、技术选型、入口/出口准则、需求假设 |
| `output/risk.md` | 5x5 风险矩阵评分（影响度 x 可能性）、分区、高风险项失效模式分析 |
| `output/testcases.md` | 结构化用例，每条一个 YAML 块 |
| `output/testcases.xlsx` | Excel 版本，列：ID/标题/优先级/类型/前置条件/步骤/预期结果/设计方法/需求追溯 |

### 手动运行脚本

```powershell
python scripts/validate_cases.py output/testcases.md --plan output/test-plan.md
python scripts/export_xlsx.py output/testcases.md --out output/testcases.xlsx --plan output/test-plan.md
```

校验通过输出 `OK`（退出码 0）；覆盖缺口以 WARNING 提示，不阻断。

## Schema 契约（一期 v1）

用例字段：`id / title / priority / type / preconditions / steps / expected / design_method / requirement_ref`。
枚举值与格式规则见 `templates/testcase.yaml`。修改契约时需同步更新：模板、qa_common.py 中的常量、两个技能文档。

## 已验证

- 示例需求（手机号注册）：18 条用例，覆盖时效/锁定边界（4/5/6 次错误、5 分钟临界），校验通过并导出 xlsx。
- 第二需求（微信登录）：12 条用例，验证技能不依赖示例硬编码。
- 负向测试：故意注入 7 类错误（枚举越界、ID 格式错、字段缺失、类型错误、引用不存在的需求等），校验脚本全部拦截。

## 二期规划（占位，一期不做）

- Playwright 自动化脚本生成与执行闭环
- AdsPower 浏览器环境集成（需先验证 CDP 连接可行性）
- Allure 报告、失败归因与用例自修复
- PICT 精确 pairwise 组合、Jira/Xray/TestRail 同步
