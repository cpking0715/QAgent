# QAgent：独立 QA 测试 Agent

从需求文档出发，按固定流水线生成 **测试需求 → 测试方案（含思维导图）→ 风险分析 → 覆盖矩阵 → 结构化测试用例 → QA Review（md/xlsx）**。

支持 **Windows / macOS / Linux**。别人拿到仓库后按顺序即可：**克隆 → 安装 Python → 建虚拟环境 → 装包 → 配 LLM Key → 选一种方式运行**。

| 方式 | 入口 | 适合谁 |
|------|------|--------|
| **A. Web 服务（推荐）** | `qagent serve` | 内网多人：先确认范围再生成、可终止、下载产物、对话修订 |
| **B. 命令行一键生成** | `qagent run` | 个人/CI，无界面，全自动 |
| **C. Cursor Skill** | `/qa generate` | 在 IDE 里对话生成，可人工介入 |
| **D. Docker** | `deploy/docker-compose.yml` | 服务器或本机 Docker Desktop |

设计细节见 [`AGENT.md`](AGENT.md)。内网/飞书部署见 [`deploy/README.md`](deploy/README.md)。

下文命令里：

- **Windows** 用 PowerShell（开始菜单搜 “PowerShell”）。若只有 CMD，把 `Copy-Item` 换成 `copy`，把 `$env:NAME="值"` 换成 `set NAME=值`。
- **macOS / Linux** 用终端（bash / zsh）。
- 激活虚拟环境后，三端都直接打 `qagent`，路径可用正斜杠（`output/ocr`）。

---

## 1. 克隆仓库

先安装 Git：

| 系统 | 安装 |
|------|------|
| Windows | [Git for Windows](https://git-scm.com/download/win)，或 `winget install Git.Git` |
| macOS | `xcode-select --install`，或 `brew install git` |
| Linux | `sudo apt install -y git`（Debian/Ubuntu）或 `sudo yum install -y git`（openEuler/CentOS） |

```bash
git clone git@gitlab.sh.sensetime.com:g2_test/applet/qagent.git
cd qagent
```

SSH 没权限时改用 HTTPS：

```bash
git clone https://gitlab.sh.sensetime.com/g2_test/applet/qagent.git
cd qagent
```

---

## 2. 安装 Python 3.9+

需要 **Python 3.9 或更高**（含 pip）。

### Windows

1. 打开 [python.org/downloads](https://www.python.org/downloads/windows/)，安装 3.11 或 3.12。
2. **务必勾选** “Add python.exe to PATH”。
3. 关掉并重新打开 PowerShell，检查：

```powershell
python --version
python -m pip --version
```

也可用：`winget install Python.Python.3.12`

Windows 上一般用 `python`，不要用 `py` 以外的别名混用；下面 Windows 示例统一写 `python`。

### macOS

```bash
brew install python
python3 --version
python3 -m pip --version
```

没有 Homebrew 时从 [python.org/downloads/macos](https://www.python.org/downloads/macos/) 安装。系统自带的 `/usr/bin/python3` 也可以，建议 3.9+。

### Linux

```bash
# Debian / Ubuntu
sudo apt update
sudo apt install -y python3 python3-venv python3-pip

# openEuler / CentOS / RHEL
sudo yum install -y python3 python3-pip
```

```bash
python3 --version
python3 -m pip --version
```

---

## 3. 虚拟环境与安装 QAgent

在仓库根目录执行。虚拟环境可以避免污染系统 Python。

### Windows（PowerShell）

```powershell
cd 你的路径\qagent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e .
qagent --help
```

若提示无法加载脚本，先允许当前用户执行本地脚本（只需一次）：

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

仍失败时用：

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\qagent.exe --help
```

CMD 激活：`.venv\Scripts\activate.bat`

### macOS / Linux

```bash
cd /path/to/qagent
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -U pip
python3 -m pip install -e .
qagent --help
```

看到 `validate` / `run` / `serve` / `mindmap` 等子命令即安装成功。

开发或跑测试再装：

```bash
python -m pip install -e ".[dev]"    # macOS/Linux 若未激活 venv：改用 python3
pytest
```

`qagent` 不在 PATH 时（三端通用兜底）：

```bash
python -m qagent.cli --help
```

---

## 4. 配置 LLM（必做，否则无法真正生成）

Key **不要提交到 Git**。三种配法任选其一。

### 方式 1：Web 设置（推荐）

`qagent serve` 打开页面 → 左下角 **设置** → 填写 API Key（以及接口地址、模型）→ 保存。会写入本机 `qagent.local.yaml`，不进 Git。

### 方式 2：本地文件

Windows PowerShell：

```powershell
Copy-Item qagent.local.yaml.example qagent.local.yaml
```

macOS / Linux：

```bash
cp qagent.local.yaml.example qagent.local.yaml
```

编辑 `qagent.local.yaml`：

```yaml
llm:
  api_key: "sk-你的key"
  model: gpt-4o-mini                    # 按实际模型改
  base_url: https://api.openai.com/v1   # 兼容网关改这里
  temperature: 0.2
  max_tokens: 8192
  timeout: 600            # 单次请求超时（秒）
  retries: 3              # 429/5xx/网络错误的重试次数（指数退避）
  backoff_seconds: 1.0    # 退避基数：1s/2s/4s
```

全局并发上限用环境变量 `QAGENT_MAX_CONCURRENT_LLM` 控制（默认 16），覆盖"多任务 × 多批次"的嵌套并发。

长任务想支持**秒级终止**：在 `qagent.local.yaml` 里设 `llm.stream: true`（默认关闭）。开启后 LLM 流式输出，终止请求会在输出 chunk 之间生效，不再等待整次调用结束（最长 600s）。

Web 界面使用 SSE 推送任务进度与日志（不再 600ms 轮询）；服务重启后，中断的任务会被标记为 failed 并提示可续跑（`from=auto` 从第一个缺失产物的步骤继续）。

### 调整生成标准（rules.yaml，摸索期常用）

所有数值规则（用例数量参考区间、清单行数、对话单次用例上限等）统一在 `templates/rules.yaml` 一处维护，prompt 与 Skill 文档都从这里渲染，改完即全局生效。原则：**用例数量由需求覆盖决定（矩阵每行、每个 R、每条边界定义），数量区间只是明显偏离时的自查参考**，不是硬性上限。

改完 `rules.yaml` 后运行一次，把 SKILL.md 的生成块同步更新：

```bash
python -m qagent.skills_gen            # 就地更新
python -m qagent.skills_gen --check    # CI 校验是否同步
```

长需求想省 token：在 `qagent.yaml` 里设 `prompt_context_mode: sliced`，批次 prompt 会按预算截断上下文、只携带本批 R 条目（默认 `full` 携带全文，行为与历史一致；预算可用 `prompt_treq_budget` / `prompt_plan_budget` / `prompt_risk_budget` 微调）。

`qagent.local.yaml` 已在 `.gitignore` 中。

### 方式 3：环境变量

Windows PowerShell（仅当前窗口）：

```powershell
$env:OPENAI_API_KEY="sk-你的key"
```

macOS / Linux：

```bash
export OPENAI_API_KEY=sk-你的key
```

公共模型参数在 [`qagent.yaml`](qagent.yaml) 的 `llm` 段，可改 `model` / `base_url`。

---

## 5. 使用方式

### 分阶段确认工作流（默认）

按日常评审节奏分三段生成，每段产物可**在线修改**后再继续：

1. **测试需求**（测什么/不测什么）：上传文档 → 确认范围 → 自动生成本段即停；点产物抽屉的「编辑」可直接修改 Markdown（需求导图随编辑同步更新），或对话让我调整；
2. **测试方案 + 风险 + 覆盖矩阵**：点「确认无误，继续下一阶段」，以上一段（含你的修改）为输入；
3. **测试用例 + QA Review + xlsx**：再次确认后生成，之后仍可对话补充修订。

每段产物也可下载离线修改后继续；后端等价于 `POST /api/jobs/{id}/run {"from": "test_plan", "stop_after": "coverage_matrix"}`，
CLI 等价于分段执行 `qagent run 需求.md --from auto`（自动从第一个缺失产物的步骤续跑）。
更多菜单里的「重新生成」为全程一键（会覆盖已有方案与用例）。

### 从中间环节开始（已有部分产物）

已经写好了测试需求 / 测试方案 / 风险 / 覆盖矩阵（甚至用例）？直接把这些文件**按原名上传**即可：

- 文件名命中 `测试需求` / `test-requirements`、`测试方案` / `test-plan`、`风险` / `risk`、`覆盖矩阵` / `coverage-matrix`、`测试用例` / `testcases` 时，上传内容会**原样作为当前产物**（不会被重新生成覆盖）；
- 跳过范围确认，自动「缺什么补什么」：只生成缺失的上游文档与后续步骤（`from: auto` 语义），已写好的产物全程复用；
- 全套产物齐备时（含用例）运行零 LLM 调用，只做评审、校验与导出；
- 更多菜单还支持从 **测试方案 / 风险 / 覆盖矩阵** 任一环开始重生成（该环及之后覆盖，之前保留）。

输入支持：`.md` `.txt` `.markdown` `.pdf` `.docx`。建议额外提供 **测试需求**（测什么、不测什么），质量会明显高于只丢 PRD。

Windows：

```powershell
Copy-Item templates\test-requirements.example.md input\test-requirements.md
```

macOS / Linux：

```bash
cp templates/test-requirements.example.md input/test-requirements.md
```

也可在 Web 上传时附加名为 `测试需求.md` / `test-requirements.md` 的文件。

### 方式 A：Web 服务（推荐）

本机（先 `cd` 到仓库根目录，并激活 `.venv`）：

```bash
qagent serve
```

浏览器打开 <http://127.0.0.1:8765/> 。

内网给同事用：

```bash
qagent serve --host 0.0.0.0 --port 8765 --no-browser
```

浏览器打开 `http://<这台机器IP>:8765/`。

- Windows 若同事打不开：系统设置 → 防火墙 → 允许 Python / 8765 入站。
- 查本机 IP：Windows `ipconfig`；macOS/Linux `ifconfig` 或 `ip addr`。

**页面操作：**

1. 把 PRD / 设计文档拖进底部输入框（或点 ＋），点发送。
2. 若未附带 `测试需求.md`，会先给出范围草稿；回复「可以 / 全量」或改范围后再生成。已附带测试需求则直接开跑。
3. 生成中可点 **终止**（等当前 LLM 调用结束后停下）。失败或已终止也能下载已写出的文件。
4. 「只跑矩阵后」仅在已有覆盖矩阵时可用，否则置灰。「重新整跑」会先确认，避免误覆盖。
5. 完成后可继续对话修订（例如「给登录补异常用例」）；跑完也会问要不要补性能 / 安全 / 兼容。
6. 「新对话」开始下一个任务；历史项悬停 × 可删除。多个任务可以并行。

可选口令（内网不想裸奔时）：

Windows PowerShell：

```powershell
$env:QAGENT_TOKEN="请换成内网口令"
qagent serve --host 0.0.0.0 --port 8765 --no-browser
```

macOS / Linux：

```bash
export QAGENT_TOKEN=请换成内网口令
qagent serve --host 0.0.0.0 --port 8765 --no-browser
```

若开了口令，浏览器第一次访问接口时会提示输入，保存在本机。

任务数据默认写在当前目录 `data/jobs/`（可用环境变量 `QAGENT_JOBS_DIR` 改）。

### 方式 B：命令行一键生成

先确认已配置 Key，再在仓库根目录、已激活 venv 后执行（三端相同）。

用仓库自带示例（最快验证安装是否成功）：

```bash
qagent run input/requirement-example.md --out output
```

指定多份文档：

```bash
qagent run /绝对路径/PRD.pdf /绝对路径/设计文档.docx input/test-requirements.md --out output/ocr
```

Windows 路径示例：`qagent run D:\docs\PRD.pdf --out output\ocr`

把文件放到 `input/uploads/` 后批量跑：

```bash
qagent run --uploads --out output
```

不调 LLM、只走 Mock（离线看流水线是否通）：

```bash
qagent run input/requirement-example.md --out output --mock
```

生成完校验：

```bash
qagent check --out output
```

只从「矩阵之后」重跑用例（已有方案/矩阵时）：

```bash
qagent run input/requirement-example.md --out output --from testcases
```

成功后，`--out` 目录里会有：

| 文件 | 含义 |
|------|------|
| `test-requirements.md` / `test-requirements.drawio` | 测试需求（Draw.io 用 diagrams.net 打开） |
| `test-plan.md` | 测试方案 |
| `risk.md` | 风险分析 |
| `coverage-matrix.md` | 覆盖矩阵 |
| `testcases.md` / `testcases.xlsx` | 用例 |
| `qa-review.md` | QA Review |

已有 `test-requirements.md` 补出 Draw.io：

```bash
qagent mindmap output/test-requirements.md -o output/test-requirements.drawio
```

### 方式 C：Cursor Skill（对话式）

三端都用 Python 脚本（不依赖 bash）：

```bash
python scripts/install_skill.py .cursor/skills
```

macOS / Linux 若未激活 venv：把 `python` 换成 `python3`。其它 IDE：

```bash
python scripts/install_skill.py .qoder/skills
python scripts/install_skill.py .claude/skills
```

仍兼容旧入口（仅 macOS / Linux）：`./scripts/install-skill.sh .cursor/skills`

装好后在对话里：

```text
/qa generate input/requirement-example.md
```

或直接说：「根据 PRD 生成测试方案和测试用例」。范围没说清时会先给出测试范围草稿，确认后再生成。

Skill 负责生成前澄清范围和人在回路；`qagent run` 一键穷举；`qagent serve` 是 Web / 飞书（同样先确认范围，可终止）。

### 方式 D：Docker 部署

先安装 Docker：Windows / macOS 用 [Docker Desktop](https://www.docker.com/products/docker-desktop/)；Linux 安装 Docker Engine + Compose 插件。

Windows PowerShell：

```powershell
cd deploy
$env:OPENAI_API_KEY="sk-你的key"
$env:QAGENT_TOKEN="请换成内网口令"
docker compose up -d --build
```

macOS / Linux：

```bash
cd deploy
export OPENAI_API_KEY=sk-你的key
export QAGENT_TOKEN=请换成内网口令
docker compose up -d --build
```

浏览器打开 `http://<服务器或本机>:8765/`。环境变量与 Nginx 反代见 [`deploy/README.md`](deploy/README.md)。

---

## 6. 其它常用命令

流水线由外部 Agent 写产物、本 CLI 做校验时：

```bash
qagent generate input/requirement-example.md --out output
qagent pipeline status --out output
qagent check --out output
qagent pipeline validate-export --out output
```

分步校验 / 导出：

```bash
qagent validate --out output
qagent export output/testcases.md --out output/testcases.xlsx --plan output/test-plan.md
```

兼容旧脚本（不保证覆盖矩阵 / Review 检查，完整校验请用 `qagent check`）：

```bash
python scripts/validate_cases.py output/testcases.md --plan output/test-plan.md
python scripts/export_xlsx.py output/testcases.md --out output/testcases.xlsx --plan output/test-plan.md
```

---

## 7. 飞书（可选）

Web 不需要公网域名。飞书机器人回调需要飞书能访问 `https://你的域名/api/feishu/event`，并配置：

Windows PowerShell：

```powershell
$env:FEISHU_APP_ID="cli_xxx"
$env:FEISHU_APP_SECRET="xxx"
$env:FEISHU_VERIFICATION_TOKEN="xxx"
qagent serve --host 0.0.0.0 --port 8765 --no-browser
```

macOS / Linux：

```bash
export FEISHU_APP_ID=cli_xxx
export FEISHU_APP_SECRET=xxx
export FEISHU_VERIFICATION_TOKEN=xxx
qagent serve --host 0.0.0.0 --port 8765 --no-browser
```

无公网域名时请用方式 A（Web），不要走事件订阅。说明见 [`deploy/README.md`](deploy/README.md)。

---

## 设计原则

- **确定性流水线**：固定顺序；先矩阵再用例，再用例后 QA Review；Step 8–9 由 CLI 强制校验与导出。
- **结构化契约**：用例 Schema 唯一事实来源为 [`templates/testcase.schema.yaml`](templates/testcase.schema.yaml)。
- **可追溯**：用例引用需求 R 编号；矩阵每行至少 1 条用例；CRITICAL 风险需 P0 用例。

修改契约只需改 schema；示例见 [`templates/testcase.example.yaml`](templates/testcase.example.yaml)。

---

## 常见问题

| 现象 | 处理 |
|------|------|
| `git: command not found` / 不是内部或外部命令 | 先按第 1 节装 Git，关掉终端再开 |
| `python` / `python3` 找不到 | 按第 2 节装 Python；Windows 重装时勾选 Add to PATH |
| PowerShell 无法运行 `Activate.ps1` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| `qagent: command not found` | 先激活 `.venv`；或 `python -m pip install -e .`；或 `python -m qagent.cli --help` |
| 未配置 LLM API Key | 页面左下角「设置」填写；或写 `qagent.local.yaml`；或设 `OPENAI_API_KEY` |
| 读取 PDF 需要 pypdf | `python -m pip install -e .`（已包含 pypdf）后重跑 |
| 上传提示「请上传 md/pdf/docx」 | 只支持这些后缀；刷新页面后再传 |
| 任务一直「生成中」、日志不再更新 | 多半是服务被重启、进程没了，点「重新整跑」 |
| 同事打不开 `0.0.0.0:8765` | 用机器局域网 IP；Windows 放行防火墙；确认 `--host 0.0.0.0` |
| 飞书收不到消息 | 内网无公网域名时改用 Web；回调必须是公网 HTTPS |

---

## 目录结构

```
qagent/
├── AGENT.md                 # 独立 Agent 清单
├── qagent.yaml              # 项目配置（可提交）
├── qagent.local.yaml.example
├── qagent/                  # Python 包与 Web 静态页
├── templates/               # Schema 与文档模板
├── skills/                  # Cursor 等技能
├── input/                   # 示例需求
├── deploy/                  # Docker / Nginx
├── scripts/                 # 安装技能、兼容脚本（含跨平台 install_skill.py）
└── tests/
```

---

## 二期规划

- Playwright 自动化、Allure 报告
- Jira / Xray / TestRail 导出插件（见 `qagent.exporters`）
