# 范围澄清、任务控制与对话体验（优化版）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 上传后先澄清测试范围再生成；任务可手动终止且不破坏已写产物；「只跑矩阵后」按上游产物门闩；失败/已终止也展示产物下载；对话修订逐条容错、跑完补问非功能；飞书同一会话再发文件建新任务。

**Architecture:** 范围澄清只在「生成前」这一闸允许对话（SKILL 固定流水线的唯一例外）。任务终止采用**双锁 + 取消标志**架构：执行锁 `job_lock` 管单次跑/聊；细粒度 `meta` 写锁 `_meta_locks` 管所有 `save_meta`；取消只拿 meta 锁写 `cancelled` 并置位取消标志，runner 在步骤/批次间隙检查标志抛 `CancelRequested`，由 `_run_pipeline` 捕获后**不再覆盖**状态。产物下载与 UI 进度与流水线状态解耦，任何非 `uploaded` 状态都展示已存在产物。`meta.json` / `feishu-chats.json` 改为原子写（temp+rename）。

**Tech Stack:** Python 3、pytest、现有 `qagent` 包（PyYAML、openpyxl）。不新增依赖，不新增 Skill 包。

**Spec:** 本计划自包含；相关设计参考 `skills/qa-orchestrator/SKILL.md`、`qagent/server/*`、`qagent/agent/runner.py`。

## Global Constraints

- 不破坏现有 9 步流水线顺序；范围澄清只在 Step 0 之前，不得插入 Step 1–9 之间。
- 新增状态 `cancelled`：`STATUSES`、`JobMeta.to_public`、`index.html` 的 `STATUS`/`isBusyStatus`、飞书文案全部同步。
- 取消语义：仅 `running` / `revising` 可取消；取消只拿 `_meta_locks`（不拿 `job_lock`），否则会被执行线程卡死。
- `save_meta` / `feishu-chats.json` 写入必须原子（写临时文件 + `os.replace`），避免 UI 轮询读到半截 JSON。
- 范围确认文件必须用 ingest 可识别名：`测试需求.md` / `test-requirements.md`（见 `ingest.TEST_REQUIREMENTS_NAMES`）。
- 提示词冲突修正：`_user_supplement_hint` 由「冲突处以 PRD 为准」改为「**必测/不测/类型以用户范围为准**」。
- 对话修订保留 `snapshot_output` 仅用于 `patch_plan` 的多条编辑原子性；`upsert_cases` 改为逐条 `normalize_case` 容错。
- 跑完追问为**静态消息**（`append_chat` 直接落盘），不走 LLM、不置 `revising`，且加「已问过」去重标志。

## File Map

| 文件 | 职责 |
|------|------|
| `qagent/server/jobs.py` | 加 `cancelled` 状态、`_meta_locks` 细粒度锁、`_cancel_flags` 标志、`save_meta` 原子写、`job_dir/.cancel` 哨兵；启动 stale 恢复 |
| `qagent/server/service.py` | 双锁重构：`start_run`/`chat`/`start_chat` 持 `job_lock`；`cancel_job` 只持 meta 锁；`_run_pipeline` 捕获 `CancelRequested` 置 `cancelled`；`on_progress` 写 `current_step` |
| `qagent/server/app.py` | 新增 `POST /api/jobs/{id}/cancel`、`GET` 状态含 `current_step`；feishu 同步走 async |
| `qagent/server/feishu.py` | 收到文件先发范围草稿再等确认（不直接 `start_run`）；聊天改用 `start_chat` 异步；支持「终止」「切换任务 <id>」 |
| `qagent/server/chat.py` | 去整批回滚：异常时按动作原子性回滚；`run_chat` 支持自动追问非功能（去重）；范围草稿生成动作 |
| `qagent/server/tools.py` | `upsert_cases` 逐条容错返回坏 ID；`read_artifact` 加同义词检索；`scope` 草稿生成 |
| `qagent/agent/prompts.py` | `_user_supplement_hint` 改用户优先；新增范围澄清 prompt |
| `qagent/agent/runner.py` | `QAgentRunner` 接收 `cancel_check` 回调，在 Step 边界与 `work()` 内部检查；新增 `CancelRequested` 异常 |
| `qagent/server/static/index.html` | pending-scope 状态、`cancelled` 展示、按后缀图标、步骤进度、门闩按钮、静态追问气泡 |
| `skills/qa-orchestrator/SKILL.md` | Step 0.5 范围澄清（唯一例外说明）；CLI 仍一键穷举 |
| `tests/test_job_control.py` / `tests/test_scope_chat.py` / `tests/test_feishu_rebind.py` | 单测 |
| `AGENT.md` / `README.md` | 文档同步 |

---

### Task 1: 双锁与 meta 原子写（取消失效的根因修复）

**Files:**
- Modify: `qagent/server/jobs.py`
- Modify: `qagent/server/service.py`

**Interfaces:**
- Consumes: 现有 `JobStore.lock`（`job_lock`）、`save_meta`、`JobMeta`
- Produces:
  - `JobStore._meta_locks: dict[str, threading.Lock]`（每个 job 一个，与 `job_lock` 分开）
  - `JobStore._cancel_flags: dict[str, bool]`
  - `JobStore.cancel_flag_path(job_id) -> Path`（`job_dir/.cancel` 哨兵，跨进程可见）
  - `JobStore.save_meta(meta)` 改为原子写（temp + `os.replace`）
  - `JobStore.set_cancel(job_id)` / `is_cancelled(job_id)` / `clear_cancel(job_id)`

**Steps:**
- [ ] 在 `JobStore.__init__` 增加 `_meta_locks`、`_cancel_flags` 两个字典与 `_locks_guard`。
- [ ] `save_meta` 使用 `write_text` 到 `meta.json.tmp` 再 `os.replace` 到 `meta.json`，全程持 `_meta_locks[job_id]`（无则创建）。
- [ ] `bind_feishu` 写 `feishu-chats.json` 同样改为原子写（temp + `os.replace`）。
- [ ] `set_cancel` 置内存标志并写 `.cancel` 哨兵；`is_cancelled` 先读内存再回退读哨兵文件。

**Checklist:**
- [ ] 任意两个线程同时 `save_meta` 同一 job 不出现半截 JSON。
- [ ] UI 连续轮询下，`get_job` 永不抛 `JSONDecodeError`。

---

### Task 2: 取消执行链路（cancel API + runner 协作）

**Files:**
- Modify: `qagent/agent/runner.py`
- Modify: `qagent/server/service.py`
- Modify: `qagent/server/jobs.py`（状态枚举）
- Modify: `qagent/server/app.py`

**Interfaces:**
- Consumes: `QAgentRunner.run`、`_run_pipeline_locked`、`on_log`
- Produces:
  - `runner.CancelRequested(Exception)`
  - `QAgentRunner.__init__(..., cancel_check: Callable[[], bool] | None = None)`
  - `service.cancel_job(job_id) -> dict`
  - `app.py`：`POST /api/jobs/{id}/cancel` → 仅 `running`/`revising` 可调，否则 400

**Steps:**
- [ ] `JobMeta` / `STATUSES` 增加 `"cancelled"`。
- [ ] `QAgentRunner` 在 `run()` 的每个 Step 开始前、以及 `_generate_matrix_batches` / `_generate_case_batches` 的 `work()` 顶部调用 `cancel_check()`，为真则 raise `CancelRequested`。
- [ ] `service._run_pipeline` 的 `except` 区分 `CancelRequested`：捕获后 `meta.status = "cancelled"`、`clear_cancel`，**不**写 `failed`；其它异常仍写 `failed`。
- [ ] `service._chat_locked` 结尾的 `if meta.status == "revising": meta.status = "ready"` 改为 `if meta.status == "revising"` 才改回，避免把 `cancelled` 刷回 `ready`。
- [ ] `cancel_job`：先 `load`，若状态非 `running`/`revising` 抛 `RuntimeError`（→ 400）；否则只持 `_meta_locks` 写 `cancelled` + `set_cancel` + `append_log("用户终止")`，**不持 `job_lock`**。
- [ ] `start_run` / `start_chat` / `chat` 进入执行前先 `clear_cancel`。
- [ ] `app.py` 增加 cancel 路由，复用 400/404 处理。

**Checklist:**
- [ ] 取消后 `status == cancelled`，已写产物保留且可下载。
- [ ] 取消不会把状态覆盖回 `ready`/`failed`。
- [ ] `cancel_job` 不阻塞在 `job_lock` 上（即使流水线正在跑也能秒回）。

---

### Task 3: 启动 stale 恢复

**Files:**
- Modify: `qagent/server/jobs.py`
- Modify: `qagent/server/service.py`（`serve` / `QAgentService.__init__`）

**Steps:**
- [ ] `JobStore.recover_stale()`：遍历 `list_jobs`，将 `running`/`revising` 标记为 `failed`，`error=["服务重启，任务未完成，请重新整跑"]`，清理 `.cancel` 哨兵。
- [ ] 仅当 `QAGENT_DISABLE_RECOVERY` 未置位时，在 `serve` 启动与 `QAgentService.__init__` 后调用一次。

**Checklist:**
- [ ] 进程被 Ctrl+C / 崩溃后重启，原先 `running` 任务不再永久卡死。

---

### Task 4: 范围澄清（Skill + Web + 飞书）

**Files:**
- Modify: `skills/qa-orchestrator/SKILL.md`
- Modify: `qagent/server/feishu.py`
- Modify: `qagent/server/tools.py`（`generate_scope_draft`）
- Modify: `qagent/agent/prompts.py`
- Modify: `qagent/server/static/index.html`（pending-scope 状态）

**Interfaces:**
- Consumes: `create_job`、`input/测试需求.md` 命名约定（ingest）
- Produces:
  - `tools.generate_scope_draft(store, job_id) -> str`：基于 `input/*.md` 用 LLM 出一次草稿（必测/不测/类型/环境/规模）
  - 飞书/Web 新动作 `confirm_scope` / `start_with_scope`

**Steps:**
- [ ] SKILL.md 在 Step 0 前插入 Step 0.5 范围澄清说明，明确「这是固定流水线唯一允许对话的闸」；跳过条件：已有 `input/测试需求.md` 或用户说「全量 / 直接生成」。
- [ ] `prompts.py` 新增 `build_scope_prompt`；`_user_supplement_hint` 改为「必测/不测/类型以用户提供范围为准，仅在用户范围缺漏处以 PRD 兜底」。
- [ ] Web 上传：`create_job` 后**不**立即 `start_run`，改为 `generate_scope_draft` 并将草稿作为助手消息 `append_chat`；job 状态仍为 `uploaded`，UI 显示「待确认范围」+ 快捷按钮（按草稿跑 / 全量 / 我要改）。
- [ ] 用户确认或说「全量」→ 写 `input/测试需求.md`（按 `templates/test-requirements.example.md` 结构，文件名必须是可识别名）→ `start_run`。
- [ ] 飞书收到文件：先 `reply_text` 范围草稿，**不** `start_run`；用户回「可以/全量/改」后调用 `start_with_scope` 写 `input/测试需求.md` 再 `start_run`。
- [ ] 超时处理：`uploaded` + 无 chat 超过阈值仅展示等待，不自动跑；提供「取消等待」= `delete_job`。

**Checklist:**
- [ ] 有用户「不测性能」时，最终 `test-requirements.md` 体现且不进入用例。
- [ ] 用户说「全量」可一键跳过澄清直接跑。
- [ ] 范围文件被重新整跑时仍生效（ingest 按文件名合并进编译文档）。

---

### Task 5: 开跑按钮门闩

**Files:**
- Modify: `qagent/server/service.py`（`start_run`）
- Modify: `qagent/server/app.py`
- Modify: `qagent/server/static/index.html`

**Interfaces:**
- Consumes: `runner._load_upstream_artifacts`
- Produces: `start_run(from_step="testcases")` 缺产物时抛 `ValueError` → 400

**Steps:**
- [ ] `start_run` 在提交线程前，若 `from_step == "testcases"`，先检查四个上游产物**存在且可解析**（`parse_coverage_matrix` / `parse_requirement_ids` 等不抛错）；缺失或不合法直接抛 `ValueError`（API 400）。
- [ ] 前端「只跑矩阵后」：根据 `job.artifacts` 含 `test_requirements` / `test_plan` / `risk` / `coverage_matrix` 且 `!isBusyStatus` 才可点，否则 `disabled` + `title` 说明缺失项。
- [ ] 前端「重新整跑」：点击先 `confirm("将清空已有产物并整跑，确认？")`，busy 时两个开跑按钮均禁用。

**Checklist:**
- [ ] 无矩阵时 `from=testcases` 返回 400，不进入 runner。
- [ ] 坏矩阵（存在但解析失败）也不点亮按钮。

---

### Task 6: 产物与进度 UI

**Files:**
- Modify: `qagent/server/service.py`（`on_progress` → `meta.current_step`）
- Modify: `qagent/server/static/index.html`

**Interfaces:**
- Consumes: `runner` 的 Step 标签、`JobMeta`
- Produces: `JobMeta.current_step: str | None` 字段；UI 步骤条

**Steps:**
- [ ] `JobMeta` 加 `current_step: str | None = None`（`to_public` 透传）。
- [ ] `service._run_pipeline_locked` 给 `QAgentRunner` 传 `on_progress=lambda s: self._set_current_step(job_id, s)`（持 `_meta_locks` 写 meta）。步骤标签复用 `PipelineStep` 名称 + 用例计数（如 `6/9 用例`）。
- [ ] UI `thinkingCard`：除 `ready` 外，`failed` / `cancelled` / `running` / `revising` 均展示 `artCards`（已有 `refresh_artifacts` 支撑）。图标按后缀：`XLSX`/`MD`/`MM`/`OPML` 区分，不再一律 `MD`。
- [ ] 顶部展示步骤条：`uploaded → 需求 → 方案 → 风险 → 矩阵 → 用例 → Review → 校验 → 导出 → 完成`，按 `current_step`/`status` 高亮。

**Checklist:**
- [ ] 失败任务能下载已生成的 xlsx/md。
- [ ] `current_step` 更新不引发 meta 读竞争（依赖 Task 1 原子写）。

---

### Task 7: 对话修订更稳

**Files:**
- Modify: `qagent/server/chat.py`
- Modify: `qagent/server/tools.py`（`upsert_cases` 逐条容错、`read_artifact` 同义词）
- Modify: `qagent/server/static/index.html`（静态追问气泡）

**Interfaces:**
- Consumes: `normalize_case`、`merge_cases`、`snapshot_output`
- Produces: `upsert_cases` 返回 `(kept: int, dropped: list[str])`

**Steps:**
- [ ] `read_artifact` 同义词：query 命中「性能」时同时匹配 耗时/SLA/吞吐/时延/秒/QPS/并发；可维护一个小同义词表。
- [ ] `upsert_cases`：逐条 `normalize_case`，捕获单条异常，把坏 ID 收集进 `dropped`，只写入合法用例；返回 `(len(merged), dropped)`。
- [ ] `chat.py` 去整批回滚：`apply_actions` 对 `patch_plan` 仍用 `snapshot_output` 包裹（多条编辑原子）；`upsert_cases`/`delete_cases` 不再整体回滚，坏用例丢弃并把 `dropped` 列表回给用户。
- [ ] `validate_and_export` 失败不再回滚已写入产物，仅把错误明细返回；UI 提示「X 条坏用例未写入」。
- [ ] 跑完追问：`run_chat` 在 `status` 转 `ready` 时，若 `meta.asked_nonfunc` 为假，追加一条**静态**助手消息（是否补性能/安全/兼容），置 `asked_nonfunc=True`（去重，重跑不再重复问）。

**Checklist:**
- [ ] 一条好用例 + 一条坏用例：`upsert` 后好的留在 md/xlsx，坏的不在，且用户收到坏 ID 说明。
- [ ] 追问不触发额外 LLM 调用、不置 `revising`。

---

### Task 8: 飞书再发文件 = 新任务 + 命令

**Files:**
- Modify: `qagent/server/feishu.py`

**Steps:**
- [ ] 收到新文件：始终 `create_job` + `bind_feishu`（改绑到新 id），旧任务产物保留；旧任务若仍在 `running` 继续并行。
- [ ] 文字消息：默认打到「当前绑定」任务；新增指令「终止」→ `service.cancel_job(当前job)`；「切换任务 <id>」→ 改绑到指定 id（校验存在）。
- [ ] 聊天改用 `service.start_chat`（异步），让修订中也能响应「终止」（依赖 Task 2 双锁）。

**Checklist:**
- [ ] 飞书第二次发文件：产生两个 job，reply 文案带新/旧 job id。
- [ ] 修订中发「终止」能在当前 LLM 请求结束后生效（不永久卡死）。

---

### Task 9: 文档与单测

**Files:**
- Modify: `AGENT.md` / `README.md`
- Create: `tests/test_job_control.py`
- Create: `tests/test_scope_chat.py`
- Create: `tests/test_feishu_rebind.py`

**Steps:**
- [ ] README/AGENT 同步取消、续跑门闩、范围澄清、飞书新任务行为。
- [ ] `test_job_control`：`cancel_job` 置 `cancelled` 且可再 `start_run`；无矩阵时 `from=testcases` 抛错（400）；`save_meta` 并发无半截 JSON。
- [ ] `test_scope_chat`：含「不测性能」时 prompt 命中用户优先；范围文件命名正确被 ingest 识别。
- [ ] `test_feishu_rebind`：第二次发文件两张 job，旧产物保留，切换/终止指令有效。

**Checklist:**
- [ ] 全部单测通过；CI 不回归。

---

## 验证总览

- 有用户「不测性能」→ 最终用例无性能项、提示词含范围优先。
- `cancel_job` → `cancelled`，可再 `start_run`；无矩阵时 `from=testcases` → 400。
- 对话 upsert 一条坏 + 一条好：好的留 md/xlsx，坏的不在且被告知。
- 飞书第二次发文件：两 job，旧产物保留；「终止」在修订中可响应。
- 上传后先出范围草稿；矩阵未齐「只跑矩阵后」灰；整跑有确认；失败任务能下载已有文件。
- 进程重启后 stale `running` 任务被标 `failed` 而非永久卡死。
