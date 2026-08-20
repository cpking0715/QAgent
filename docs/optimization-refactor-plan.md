# QAgent 优化与重构方案

> 日期：2026-08-20 ｜ 状态：**阶段 0-4 全部实施完毕，端到端验证通过（124/124 测试）** ｜ 范围：skills 知识体系、流水线工作流、服务端架构、性能、工程化
> 基准代码：main 分支 `366d12f`（所有 `file:line` 引用以该版本为准）
> 最终落地摘要（2026-08-20）：
> - 阶段 0/1：删死代码与双份打包、模板/schema/静态页缓存、ingest 去重、SC 正则统一；meta 原子写 +
>   内存索引 + per-job 锁（update()）、取消竞态修复、日志 append-only（logs.txt）、飞书 token 缓存（飞书已确认暂缓接入）。
> - 阶段 2：数值规则收敛到 `templates/rules.yaml`（覆盖优先、区间仅参考），prompt/chat/SKILL.md 同源渲染，
>   `python -m qagent.skills_gen --check` 同步校验；LLM 重试/退避/全局限流；批次 prompt 切片开关（默认 full）。
> - 阶段 3：校验统一 `validation.full_validate`（三处复制消除）；`run()` 拆为步骤状态机 + `_FlowState` +
>   `on_step` 结构化进度（`_STEP_RE` 反解析已删）；步级断点续跑（`--from auto|步骤名`）；矩阵行/用例结构化直传。
> - 阶段 4：`cgi` 移除（手写 multipart，解锁 Python 3.12+）；SSE 推送（前端订阅 + token 走 query）；
>   chat 与 pipeline 线程池分离；`llm.stream` 流式 + chunk 间取消（秒级终止）；服务重启 stale 标记；
>   对话修订 read_artifact 结果回流（最多两轮）；任务列表按 owner 过滤。
> - 端到端验证：`tests/test_e2e.py` 覆盖 HTTP 全链路（上传→范围澄清→**三段式生成（段间人工编辑产物）**→SSE→下载→对话修订→删除）
>   与 CLI 全链路（`run --mock` → 8 类产物齐全）。
> 追加（同日，用户需求）：**分阶段确认工作流**——runner 支持 `stop_after` 停止点（RunResult.stopped_after），
> service 按产物存在性推导 `stage`（下一步动作），`PUT /api/jobs/{id}/artifacts/{name}` 支持在线编辑 Markdown 产物
> （需求导图随编辑更新），前端提供「确认无误，继续下一阶段」按钮与产物编辑抽屉；
> 上传/范围确认后默认只生成测试需求段，对应 tests/test_phase5.py。
> 进度备注（2026-08-20）：阶段 0/1 见提交记录（删死代码、原子写、取消竞态、append-only 日志等）；
> 阶段 2 已落地——数值规则统一到 `templates/rules.yaml`（覆盖优先、区间仅参考），prompt/chat/SKILL.md 均从其渲染，
> `python -m qagent.skills_gen --check` 做同步校验；LLM 客户端已支持重试/退避/全局限流（`QAGENT_MAX_CONCURRENT_LLM`）；
> 批次 prompt 切片以 `prompt_context_mode: sliced` 开关提供，默认关闭。
> 范围调整（2026-08-20）：**飞书接入确认暂不需要，聚焦本地 agent（CLI + 本地 Web）**——
> 飞书代码保留但不再投入（W-08/P-04 的飞书部分、阶段 4 的飞书异步化均标记暂缓）；
> 阶段 4 剩余聚焦：SSE、取消事件化、池分离、cgi 替换、重启恢复。
> 进度备注（阶段 3 首批，2026-08-20）：校验编排统一为 `validation.full_validate`
> （CLI/Runner/tools 三处复制消除，tools 不再实例化 Runner）；`run()` 拆为步骤状态机
> （`_step_*` + `_FlowState`），进度经 `on_step` 结构化回调上报（`_STEP_RE` 日志反解析已删）；
> 步级断点续跑落地（`--from auto|步骤名`，复用产物补记 pipeline 状态）。
> 阶段 3 剩余：结构化中间产物（flow/models + render，需真实任务对比补桩率后再切换）。
> 结论先行：本方案允许较大重构（已确认），主线是 **"消灭四份知识副本、引入结构化中间产物、重做任务存储层、升级 LLM 客户端与服务层"**，分 5 个阶段落地。

---

## 0. TL;DR：十个最值得修的问题

| # | 问题 | 一句话影响 | 对应章节 |
|---|------|-----------|---------|
| 1 | 领域知识存在 4 份副本且**数值互相矛盾** | Cursor 路径与 runner 路径产出标准不一致，改一条规则要同步 3~4 处 | S-01/S-02 |
| 2 | `templates/` 与 skill 内模板 7 份文件 100% 重复 | 靠人工同步，`config.py` 被迫写三级路径回退 | S-01 |
| 3 | `meta.json` 读-改-写竞态 + 非原子写 | 任务偶发"消失"、**取消请求可能被覆盖丢失** | W-01/W-02 |
| 4 | 每条日志 = 全量读+全量写 meta.json | 一次流水线数百次 JSON 序列化读写，并与取消/修订写互相覆盖 | P-02 |
| 5 | Web UI 600ms 轮询触发全量目录扫描 | 任务积累后 `/api/jobs` 成为瓶颈 | P-01 |
| 6 | 步骤间以"Markdown + 正则"耦合 | `parsing.py` 801 行大半在兜底 LLM 输出，实测 110 条用例中 39 条为脚本补桩 | W-05 |
| 7 | 批次 prompt 重复携带全量上游产物 | token 成本与延迟随批次数线性放大 | P-05 |
| 8 | 8 job × 8 批 = 64 路并发 LLM，无全局限流 | 网关被打爆、交互式 chat 被流水线饿死 | P-07 |
| 9 | `qagent/serve.py` 400 行死代码 + `import cgi`（Python 3.13 已移除） | 误导维护者，升级基础镜像即崩 | Q-01/Q-05 |
| 10 | 校验逻辑三处复制，`tools.py` 为复用校验专门实例化 Runner 调私有方法 | 修一处漏两处，边界腐蚀 | Q-02 |

---

## 1. 现状概览

### 1.1 项目定位

从 PRD/需求文档出发的 QA 测试文档生成 Agent，固定 9 步流水线：

```
摄入(PDF/docx/md → requirement.md)
  → Step 2 测试需求 test-requirements.md (+drawio/xmind)
  → Step 3 测试方案 test-plan.md
  → Step 4 风险 risk.md
  → Step 5 覆盖矩阵 coverage-matrix.md（16 R/批 × 8 路并行 + 校验修复循环）
  → Step 6 测试用例 testcases.md（12 行/批 × 8 路并行 + 脚本补齐）
  → Step 7 QA Review qa-review.md（纯脚本渲染追溯表）
  → Step 8 校验修复循环（脚本补齐 → LLM 定向修复，retry 3）
  → Step 9 导出 testcases.xlsx
```

四种使用入口共享同一条流水线：A. `qagent serve` 内网 Web（多人并行、范围澄清、可终止、对话修订）；B. `qagent run` CLI 一键；C. Cursor Skill `/qa generate`（IDE Agent 按 `skills/qa-orchestrator/SKILL.md` 分步执行）；D. Docker（nginx 反代）。

### 1.2 代码规模

| 模块 | 行数 | 说明 |
|---|---|---|
| `qagent/parsing.py` | 801 | 4 套解析器 + 渲染器 + YAML 修复 + 需求推断，全项目最大 |
| `qagent/agent/runner.py` | 641 | 9 步编排 + 批次并行；`run()` 单函数 205 行 |
| `qagent/cli.py` | 468 | 9 个子命令 |
| `qagent/serve.py` | 400 | **死代码**（见 Q-01） |
| `qagent/agent/prompts.py` | 414 | 全部内置 prompt（与 skills 知识重复） |
| `qagent/server/` | ~1280 | app/service/jobs/tools/chat/feishu/scope/auth 七模块 |
| `qagent/exporters/mindmap.py` | 380 | drawio/xmind/opml 三种导图 |
| `qagent/server/static/index.html` | 659 | 单文件 SPA（CSS/JS 全内嵌） |
| `skills/`（3 个） | ~700 | orchestrator 159 + test-design 112 + testcase-generator 105 |

依赖极简（pyyaml/openpyxl/pypdf/python-docx），无 Web 框架、无 HTTP SDK、无 logging 框架。

### 1.3 现状架构

```
浏览器 index.html（600ms 轮询）
   │  POST /api/jobs · /chat · /run · /cancel
   ▼
app.py（http.server 手写路由，cgi 解析 multipart，每请求一线程）
   ▼
QAgentService（单 ThreadPoolExecutor(8)：pipeline + chat 共用）
   ├─ JobStore（data/jobs/<id>/，meta.json 全量读改写，每 job 一把锁）
   ├─ chat.run_chat（LLM 出 JSON 动作 → 快照整目录 → 脚本改文件 → 校验 → 回滚）
   └─ _run_pipeline（持 job_lock）→ ingest → QAgentRunner.run()
        ├─ prompts.py 拼装（上游产物全文 + 模板 + schema）
        ├─ OpenAILLM（urllib 单发，timeout=600，无重试/流式）
        ├─ 矩阵批/用例批 ThreadPoolExecutor(8)（嵌套并发）
        └─ parsing.py 正则解析 Markdown ←→ 落盘 .md ←→ 下一步再解析
飞书回调 → service.chat()（同步等 LLM 最长 600s）
```

---

## 2. 问题清单（含证据与影响）

严重度：🔴 阻碍演进/有正确性风险 ｜ 🟠 明显性能或维护负担 ｜ 🟡 改进项

### 2.1 Skill / 知识体系：单一事实来源缺失

**S-01 🔴 模板 7 份文件 100% 字节级重复，靠人工同步**
`templates/` 与 `skills/qa-orchestrator/templates/` 下 `risk.md`、`testcase.md`、`qa-review.md`、`testcase.example.yaml`、`coverage-matrix.md`、`testcase.schema.yaml`、`test-plan.md` diff 完全相同（约 350 行 ×2）。`docs/superpowers/specs/2026-08-18-qa-coverage-review-design.md` §3.1 表明"模板复制进 skill"是当时的设计决定，但没有任何机制保证同步；`qagent/config.py:224-235` 的 `resolve_path` 被迫实现 workspace → skill → REPO_ROOT 三级回退来容忍双份。
**影响**：改 schema 必须改两处，漏一处则 Cursor 路径与 runner 路径行为分叉。

**S-02 🔴 同一规则 4 处副本且数值互相矛盾**
- 用例总量：`skills/qa-testcase-generator/SKILL.md:53` 写"单个功能用例总数一般 **10~50 条**"；`qagent/agent/prompts.py:200`（TESTCASE_QUALITY_GUIDE）写"复杂系统 **30~80 条**；简单功能 **15~40 条**"。两条路径执行不同标准。
- 技术选型表：`skills/qa-test-design/SKILL.md:29-37` ≈ `prompts.py:151-175`（ANALYSIS_GUIDE）。
- 风险规则/表头：`skills/qa-test-design/SKILL.md:43-51` ≈ `prompts.py:210-229`（build_risk_prompt 硬编码表格列名）。
- 覆盖规则：`skills/qa-testcase-generator/SKILL.md:46-53` ≈ `prompts.py:330-374`。
- 示例 YAML 存在 4 份：`templates/testcase.example.yaml`、skill 内拷贝、`skills/qa-testcase-generator/examples.md:5-24`、同目录 SKILL.md:18-41 内嵌。

**影响**：这是"skill 还是工作流该优化哪"的根源——它们是同一知识库的两套手抄本，任何规则演进都要人肉同步 2~4 处，且已经出现矛盾。

**S-03 🟠 枚举契约硬编码漂移**
`templates/testcase.schema.yaml:1-2` 自称"机器可读，唯一事实来源"，但 `qagent/server/chat.py` 的 SYSTEM prompt 手写"type 只能是 功能/边界/异常/安全/组合"与 design_method 规则；`prompts.py:110-113` 又手写"简单功能 ≥8 行，复杂 ≥25 行"等数值。schema 改动不会传播到这两处。

**S-04 🟠 SKILL.md 与步骤编号体系多处不一致**
- `skills/qa-orchestrator/SKILL.md:40-41`：两行重复的 "Step 2" checkbox（加 drawio 时未删旧行）。
- 步骤编号三套：SKILL.md 用 Step 0.5/0/1-9；`AGENT.md:44-56` 架构图从 Step 2 起画且漏 drawio/xmind 产物；死代码 `qagent/serve.py:29-37` 还是 7 步旧编号，其正则 `Step (\d)/7` 与 runner 实际日志 `Step N/9` 永远匹配不上。
- 运行说明在 README §4-5、AGENT.md、SKILL.md 三处重叠。

**S-05 🟡 输入/输出产物命名冲突**
用户上传的范围声明文件叫 `测试需求.md`/`test-requirements.md`（`qagent/ingest.py:12-17`），流水线**生成的**产物也叫 `test-requirements.md`（`qagent/config.py:107-108`）。目录不同（input/ vs output/）但概念同名，SKILL.md 与 prompts.py 都要靠专门提示词向 LLM 解释二者区别。

### 2.2 工作流与架构

**W-01 🔴 meta.json 读-改-写竞态**
所有状态更新都是 `load() → 改字段 → save_meta()` 全量重写（`jobs.py:114-127`），无版本控制、不加锁的场景遍布：
- 流水线线程在 `service.py:122-135/156-160/171-186` 持 `job_lock` 期间反复 load→save；
- `cancel_job`（`service.py:107-114`）从 HTTP 线程 load→改→save，**不持 `job_lock`**——流水线线程随后用旧 meta 覆盖写回，`cancel_requested=True` 被吞掉，**用户点终止没反应**；
- `save_upload`/`bind_feishu`（`jobs.py:186-196, 218-243`）同样无锁。
**影响**：取消失效、状态回跳、字段互相覆盖。`docs/superpowers/plans/2026-08-20-qa-scope-clarify-control.md` 已把"meta 原子写 + 细粒度锁 + 取消哨兵"列为未实施改进，与本文判断一致。

**W-02 🔴 meta.json 非原子写 → 任务凭空消失**
`save_meta` 用 `write_text` 直接覆盖（`jobs.py:114-119`）。进程被 kill 时可能留下截断的 JSON；`list_jobs` 遇 `JSONDecodeError` 直接 `continue`（`jobs.py:160-161`），该任务从 UI 上消失。

**W-03 🟠 进程重启无恢复，两套状态互不打通**
running/revising 只存 meta.json，服务重启后任务永远停在"生成中"（README FAQ 承认只能整跑重跑）。`.qagent-pipeline.json` 记录了 9 步完成度（`pipeline.py:71-79`）但只服务 CLI `pipeline status`，与 Web job 状态、与 `start_run(from="testcases")` 的续跑判断（`jobs.py:245-247` 要求 4 个上游文件齐全）完全独立。

**W-04 🟠 进度展示靠日志字符串反解析**
`service.py:25` 定义 `_STEP_RE` 从日志文本抠 "Step x/9" 更新 `current_step`（`service.py:156-160`）。展示层与日志文案强耦合，多语言/改文案即断。

**W-05 🔴 步骤间以"Markdown 文件 + 正则"耦合，解析兜底占大头**
下游步骤全部靠解析上游 Markdown 表格/代码块：`parsing.py:472`（```requirements 块）、`parsing.py:737`（依赖 "## 1. 覆盖契约" 标题字面量）等。LLM 输出稍偏格式就靠 `repair_llm_yaml`（`parsing.py:94`）、`normalize_case`、`fill_missing_cases` 大量兜底。实测数据：某任务 110 条用例中 39 条是脚本补的桩（`data/jobs/75abee1379c34d4a/meta.json:37`）。801 行的 `parsing.py` 大半在做"解析自己渲染的 Markdown"。
**影响**：这是质量问题（补桩用例质量低）与维护问题（解析/渲染/修补三套代码同步演进）的共同根源。

**W-06 🟠 对话修订是"单发 JSON 动作"，上下文过薄且不回流**
修订上下文只含 R 编号清单摘要 + 用例条数（`chat.py:120-139`），**不含用例正文**；`read_artifact` 的结果仅作为 note 展示，不会进入下一次 LLM 调用（`chat.py:165-230`）。用户说"把 TC-REG-003 的预期改成……"这类精确指令时模型看不到用例原文，只能猜。

**W-07 🟠 取消粒度太粗 + 单次 LLM 调用 600s 不可中断**
协作式取消只在日志/批次边界检查（`runner.py:160-162` 等）；`OpenAILLM` 用 urllib 阻塞调用 timeout=600（`llm.py:50`），期间"终止"无效。README 明说"等当前 LLM 调用结束后停下"。

**W-08 🟠 飞书回调同步阻塞（暂缓：已确认暂不接入飞书）**
`app.py:147-155` 在 HTTP handler 线程内同步调 `handle_feishu_event` → `service.chat`（`service.py:209-229`，持锁等 LLM 最长 600s）。飞书事件要求秒级 ACK，必然超时重推；`deploy/nginx.conf:16` 的 `proxy_read_timeout 3600s` 就是为绕过它打的补丁。

**W-09 🟡 修订快照整目录复制且成功后不清理**
`snapshot_output` 复制整个 output 目录（含 xlsx/xmind/drawio 二进制，`tools.py:207-221`），修订成功后 `.snapshot` 一直留在 output 里（下次修订覆盖）。

**W-10 🟡 安全面**
鉴权是可选共享口令 `QAGENT_TOKEN`（`auth.py`）；owner 仅取自可伪造的 `X-User` 头；`list_jobs` 实际传 `None` 不过滤 owner（`app.py:118`）——内网多人口径下所有人共享全部任务，任何人可删除他人任务。

### 2.3 性能（按影响排序）

**P-01 🟠 轮询读放大**
前端每 600ms `select(id)` → `GET /api/jobs/{id}` + `refreshJobs()` → `GET /api/jobs`（`index.html:493-495`）。服务端：`list_jobs` 对每个任务读一遍 meta.json（`jobs.py:153-165`），`service.py:77-81` 每个 job 再做 `can_resume_from_matrix`（4 次 stat）；单 job 查询还要全量读 `chat.jsonl` 再切片 `[-40:]`（`jobs.py:207-216`）。且 `ThreadingHTTPServer` 默认 HTTP/1.0 无 keep-alive，每次轮询新建 TCP 连接。
**影响**：任务积累到几百个后，`/api/jobs` 成为服务主要负载；M 个浏览器 × N 个任务每 0.6 秒全量扫描。

**P-02 🟠 日志写放大**
每条日志：`_log` → `should_cancel()` **读盘** load meta（`service.py:162-163`）→ `append_log` 再 load 全量 meta → append → **全量写回**（`jobs.py:167-172`）→ 命中 Step 正则再 load+save 一次（`service.py:156-160`）。一次流水线数百条日志（矩阵/用例每批都打），即数百次全量 JSON 序列化读写，且与取消/修订的写并发互相覆盖（见 W-01）。

**P-03 🟠 摄入重复解析（PDF 解析 ×2）**
`ingest()` 先对 test_paths 逐个 `read_document`（`ingest.py:170-171`），再调 `merge_documents` 对全部路径（含同样的 test_paths，`ingest.py:128`）重新 `read_document`（`ingest.py:144`）。pypdf 逐页提取做两遍。且 `service.py:142-147` 每次运行（包括 `from_step="testcases"` 续跑）都重新 ingest。

**P-04 🟠 LLM 客户端裸奔**
urllib 单次 POST：无重试、无退避、无连接池、无流式（`llm.py:24-59`），timeout 硬编码 600。瞬时网络抖动/限流 429 直接让整步失败进修复循环（放大调用量）。飞书 tenant_token 每条消息重新获取（`feishu.py:30-31`，实际有效期约 2 小时）。

**P-05 🟠 批次 prompt 全量携带上游产物（token 放大）**
`build_testcases_prompt`（`prompts.py:330-374`）把 test-requirements + test-plan + risk + 矩阵切片**全文**塞进 user prompt；`runner.py:272-313` 按 12 行/批调用 → N/12 次重复发送全量上下文；矩阵阶段同理（每批带三份文档全文）。截断只有零散魔数：`prompts.py:136` 的 `[:8000]`、`prompts.py:219` 的 `[:5000]`，无 token 计数。同时每批构建都重新读模板与 schema（`prompts.py:337-338`）。
**影响**：token 成本与首字延迟随批次数线性放大；大需求文档有超上下文风险。

**P-06 🟠 解析与配置零缓存**
- 一条修订消息带 3 个 action：`job_config` → `resolve_config()`（向上找 workspace + 多路径探测 + 读 2-3 个 YAML，`config.py:197-264`）执行 3 次；`tools.py` 的 `upsert_cases`/`delete_cases`/`validate_and_export` 各一次。
- `validate_and_export` 内部：`parse_cases`/`parse_coverage_matrix`/`parse_requirement_items` 各解析 2-3 遍，`export_cases_xlsx` 导出 **2 次**，drawio+xmind 每次全量重生成（`tools.py:161-204`）。
- `runner.py:576-618` Step 8 每轮重试重新从磁盘解析全部 5 个产物文件。
- `GET /` 每次从磁盘读 28KB index.html（`app.py:24-32`）。

**P-07 🟠 嵌套线程池并发失控**
service 层 8 并发 job（`service.py:55`，`QAGENT_MAX_PIPELINE` 可调）× runner 层 8 路批（`runner.py:59`）= 最多 64 路并发阻塞式 LLM 调用，每路还可能触发 fix 循环放大。无全局信号量/队列/熔断。且 pipeline 与 chat 共用同一个池：8 个长流水线会把交互式 chat 饿死。

**P-08 🟡 Draw.io 布局 O(n²)**
`_drawio_subtree_height` 无记忆化，`_drawio_place`（`mindmap.py:260-289`）对每个节点及每个 child 重复递归计算子树高度，深链状大纲退化为 O(n²)。artifact 下载 `read_bytes()` 全量进内存（`app.py:133`）。

### 2.4 代码质量与工程化

**Q-01 🔴 400 行死代码 `qagent/serve.py`**
`cli.py:322` 实际导入 `qagent.server.app`（已验证），serve.py 全仓库无引用。内含 153 行 f-string 拼的内嵌 HTML 页面、模块级 `_run_state` 全局字典、过时的 7 步状态表与永远匹配不上的正则——纯误导。

**Q-02 🔴 校验逻辑三处复制**
`cli.py:23-89` `_run_validate` ≈ `runner.py:377-435` `_full_validate`（矩阵/Review 存在性检查、四组 validate、错误聚合几乎逐行相同）；`tools.py:193-194` 更是为复用校验专门 `QAgentRunner(config, MockLLM({}))` 然后调私有方法 `_full_validate`——校验应是无状态独立模块。

**Q-03 🟠 单函数过长 / 职责混杂**
`runner.py:437-641` `run()` 205 行串联 Step 2-9（prompt 构建、LLM 调用、批次并行、校验重试、修复循环、导出）；`cli.py:196-235` `cmd_run` 四个几乎相同的摄入分支；`jobs.py` 的 `load` 与 `list_jobs` 重复"过滤已知字段重建 JobMeta"逻辑。

**Q-04 🟠 零 logging、吞错**
全项目无 `logging`，全靠 print（CLI 45 处、runner/app 若干），服务模式下用户日志、运维日志、调试输出混在 stdout。吞错点：`service.py:236-239` `except RuntimeError: pass`（rerun 失败静默）；`jobs.py:160-161` 坏 meta 跳过；`feishu.py:48-51` 回复失败无记录。

**Q-05 🔴 `import cgi` + 双份打包声明**
`app.py:5`（及死代码 serve.py）用 `cgi.FieldStorage` 解析 multipart——cgi 在 3.11 deprecated、**3.13 已移除**，而 `pyproject.toml:10` 声明 `requires-python >=3.9`，CI/Docker 用 3.11，升级基础镜像即崩。同时 `setup.cfg:8-10` 与 `pyproject.toml:11-16` 双份打包声明且依赖不一致（setup.cfg 缺 pypdf/python-docx），`deploy/Dockerfile:13` 又手动补装——三处互相打补丁。

**Q-06 🟠 魔法数字与 cwd 依赖**
`CASE_BATCH_SIZE=12 / MATRIX_REQ_BATCH=16 / MAX_WORKERS=8`（`runner.py:57-59`）、timeout=600、截断 6000 字、logs `[-200:]`/对外 80 条、chat limit 8/40 全部硬编码不可配置。jobs 根目录 `Path.cwd()/"data"/"jobs"`（`jobs.py:250-254`）与静态页 `Path.cwd()/"qagent"/"server"/"static"`（`app.py:26`）依赖启动目录，换目录启动直接找不到。

**Q-07 🟡 重复正则与兼容垫片**
`SC-\d{3}` 正则三处各写一份（`runner.py:60`、`validation.py:15`、`parsing.py` 内）；requirements 块正则在 `tools.py:94` 与 `parsing.py:475` 各一份；`scripts/qa_common.py` 保留 `REQUIRED_FIELDS = None # 已废弃` 式垫片与 3 个纯转发壳脚本。

**Q-08 🟡 测试缺口**
6 个测试文件 82 条用例，parsing/validation/export/JobStore/HTTP 层覆盖良好，MockLLM 设计合理。缺口：runner 批次并行/取消/续跑分支、并发竞态（W-01/P-07 场景）、飞书真实回调时序、chat action 结构校验（目前 `chat.py:79-116` 只做 `str(action.get(...))` 弱转换，无 TypedDict/Pydantic）。

---

## 3. 目标架构

### 3.1 设计原则

1. **单一知识源（SSOT）**：所有领域规则只有一份机器可读定义，其余（prompt、SKILL.md、校验器）全部从它派生或消费它。
2. **结构化中间产物**：流水线内部传 dataclass/JSON，Markdown 仅为渲染输出；解析只发生在 LLM 输出边界一次。
3. **状态即文件，写必原子**：所有落盘状态原子写（tmp+rename），读-改-写必须持锁或走内存索引。
4. **取消与进度是事件，不是日志文本**：`threading.Event` 表达取消，回调上报进度，不做字符串反解析。
5. **LLM 调用是有预算的资源**：全局并发上限、重试退避、上下文按批切片。

### 3.2 目标分层与目录结构

```
qagent/
├─ knowledge/                  # ★ 新：唯一知识源（YAML/MD，机器可读）
│   ├─ rules.yaml              #   数值规则（用例数区间、风险阈值、覆盖深度…）
│   ├─ techniques.md           #   技术选型表/风险方法/覆盖规则正文
│   ├─ schema.yaml             #   用例字段+枚举（原 templates/testcase.schema.yaml）
│   └─ templates/              #   各产物模板（原 templates/，唯一一份）
├─ llm/
│   └─ client.py               # ★ 重写：重试/退避/连接池/流式/全局信号量
├─ flow/                       # ★ 新：结构化产物与步骤定义
│   ├─ models.py               #   Requirement/PlanItem/RiskItem/CoverageRow/TestCase dataclass
│   ├─ render.py               #   dataclass → Markdown/YAML 渲染（唯一渲染点）
│   └─ steps.py                #   Step 注册表：id/名称/输入输出/执行/校验/可续跑
├─ runner.py                   # 瘦身为状态机：加载 steps → 逐步执行 → 断点恢复
├─ validate/                   # ★ 新：从 cli/runner/tools 三处合并的独立校验模块
├─ server/
│   ├─ store.py                # ★ 重做：内存索引+细粒度锁+原子写+日志 append-only
│   ├─ events.py               # ★ 新：SSE 推送（进度/日志/状态变更）
│   ├─ chat.py                 # 升级：多轮工具循环，read_artifact 结果回流
│   └─ ...
└─ skills_gen.py               # ★ 新：从 knowledge/ 生成 3 个 SKILL.md（构建期/安装期）
skills/                        # 产物：由 skills_gen 生成，不再手抄模板
```

### 3.3 各层设计要点

**知识层（解决 S-01~S-04）**
- `knowledge/rules.yaml` 收敛所有数值规则（如 `case_count: {simple: [15,40], complex: [30,80]}`），`prompts.py` 渲染 prompt、`skills_gen.py` 生成 SKILL.md、校验器读取上限，**同源消除矛盾**。
- `templates/` 只保留根目录一份；skill 安装脚本（`scripts/install_skill.py`）改为软链或在安装时从 knowledge/ 复制并记录内容哈希，`config.resolve_path` 三级回退简化为一级。
- schema 的 enum/pattern 由校验器直接消费，`chat.py` 与 prompts 从 schema 动态拼提示词段落，不再手写枚举。
- 步骤编号只在 `flow/steps.py` 定义一处，SKILL.md、AGENT.md 架构图、README 均由它生成或引用。

**数据层（解决 W-05/W-06）**
- LLM 输出仍允许 Markdown/YAML（对模型友好），但**每步只在产出边界解析一次**为 dataclass；后续步骤直接消费结构化对象；`render.py` 负责统一渲染落盘（渲染格式自控，可测试）。
- `parsing.py` 中"解析自己渲染的 Markdown"的代码（约大半）随迁移删除；保留的仅剩 LLM 输出净化（repair_llm_yaml/normalize_case）。
- 对话修订上下文直接传结构化用例（按相关性筛选 N 条全文），`read_artifact` 结果回流进入多轮循环（最多 K 轮，防失控）。

**编排层（解决 W-03/W-04、Q-03）**
- `run()` 拆为 Step 对象流水线：每步声明 `id / deps / execute(ctx) / validate(ctx) / resume_from(ctx)`。
- `.qagent-pipeline.json` 与 job 状态打通：服务重启时扫描 running 状态 job → 按 pipeline 状态自动恢复或标记 stale（作者在 2026-08-20 计划文档中已有此意向）。
- 进度经回调结构化上报（step id + 百分比），删除 `_STEP_RE` 日志反解析。

**存储层（解决 W-01/W-02、P-01/P-02）**
- JobStore 启动时建内存索引（job_id → meta），运行期读走内存、写走"内存更新 + 原子落盘"；`_meta_locks` 每 job 一把细粒度锁，所有读-改-写必须持锁。
- 日志改为 append-only `logs.jsonl`（或 `logs.txt`），meta 只存计数与指针；对外接口从文件 tail 读取。
- `/api/jobs` 列表直接来自内存索引（含 mtime 失效兜底），不再每 0.6s 全量扫盘。

**LLM 客户端层（解决 P-04/P-05/P-07）**
- `complete()` 包装：429/5xx/网络错误指数退避重试（3 次），全局 `threading.BoundedSemaphore(QAGENT_MAX_CONCURRENT_LLM)` 限流，连接复用。
- 批次 prompt 上下文按批切片：矩阵批只带本批 R 条目 + 需求相关节选；用例批只带本批矩阵行 + 计划摘要 + 规则（全文改引用式摘要），模板/schema 进程内 `lru_cache`。
- 可选流式：对 Web 长步骤暴露首字进度，配合 SSE。

**服务层（解决 W-07/W-08、P-01/P-07）**
- pipeline 池与 chat 池分离（chat 池小而快，如 4；pipeline 池受全局 LLM 信号量约束）。
- 取消改 `threading.Event`（内存为主，meta 落盘为辅以支持重启语义），LLM 调用前检查；流式改造后可在 chunk 间检查，取消延迟从最长 600s 降到秒级。
- SSE 端点 `/api/jobs/{id}/events` 推送状态/日志/进度，前端列表页降频轮询（如 10s）或仅刷新时拉取。
- 飞书事件先 ACK 再入队处理（异步），token 加缓存（TTL 90min）。
- `cgi.FieldStorage` 替换为基于 `email` 或手写的 multipart 解析（或直接引入 `multipart` 小依赖）；index.html 读进内存缓存。
- 鉴权升级：`X-User` 签名或至少任务创建者与 owner 过滤落地。

**工程化（解决 Q-01/Q-04~Q-07）**
- 删 `qagent/serve.py`；`setup.cfg` 并入 `pyproject.toml`（依赖取并集），Dockerfile 去掉手动补装。
- 引入 `logging`：`qagent` 根 logger + 服务端按 job 绑定 handler，删除吞错点（至少记 warning）。
- 魔法数字进 `knowledge/rules.yaml` 或 config：批次大小、workers、timeout、日志截断、chat 轮数等。
- 路径锚定包位置而非 `Path.cwd()`（`importlib.resources` / `Path(__file__)`），jobs 根仅作显式配置项。
- chat action 定义 TypedDict + 显式字段校验函数（不必引入 pydantic，保持零重依赖风格）。

### 3.4 目标架构图

```
浏览器（SSE 订阅状态/日志/进度；列表低频刷新）
   ▼
app.py（http.server，无 cgi；静态资源内存缓存）
   ▼
QAgentService
   ├─ chat 池(4) ── chat.py 多轮工具循环（结构化上下文 + read 回流）
   ├─ pipeline 池(N) ── runner 状态机（flow/steps.py 注册表）
   │      ├─ llm/client.py（重试+退避+流式+全局信号量）
   │      ├─ flow/models.py ⇄ render.py（结构化 ⇄ Markdown 单向渲染）
   │      └─ validate/（独立校验，CLI/runner/tools 共用）
   └─ store.py（内存索引 + per-job 锁 + 原子写 + logs.jsonl append-only）
knowledge/（rules+schema+templates 唯一知识源）
   ├→ prompts 渲染（运行时）
   └→ skills_gen 生成 skills/（安装期）
```

---

## 4. 分阶段实施路线图

> 原则：每阶段独立可合入、可回归；阶段 0-1 对四种使用入口零行为变化；阶段 2-3 内部换引擎、外部接口不变；阶段 4 引入前后端协议变化并给迁移说明。

### 阶段 0：快赢清理（约 0.5~1 天）

| 任务 | 涉及文件 | 说明 |
|---|---|---|
| 删除死代码 `qagent/serve.py` | `qagent/serve.py` | 全仓库无引用（`cli.py:322` 已指向 server.app），连同其测试（如有）删除 |
| 合并打包声明 | `setup.cfg` → 删除，`pyproject.toml` | 依赖取并集（补 pypdf/python-docx），Dockerfile 去手动补装 |
| 修 SKILL.md 重复 Step 2 行 | `skills/qa-orchestrator/SKILL.md:40-41` | 删旧行，统一 0.5/0-9 编号 |
| 模板/schema/静态资源缓存 | `prompts.py`、`app.py` | `_read`/`load_schema`/index.html 加进程内缓存 |
| meta.json 原子写 | `jobs.py:114-119` | `write_text` 改 tmp+`os.replace` |
| ingest 去重读 | `ingest.py:154-184` | 已读文本传入 `merge_documents`，PDF 只解析一遍 |
| 统一 SC 正则 | `runner.py:60`、`validation.py:15`、`parsing.py` | 收敛到一处常量 |

**验收**：`pytest` 全绿；`qagent run`/`serve`/`validate`/`export`/`check`/`mindmap` 行为不变；grep 确认无 `import cgi` 于存活代码（serve.py 删除后 app.py 仍在，此处只删死代码，cgi 替换在阶段 4）；`pip install -e .` 后依赖完整。
**回归风险**：低。ingest 去重注意保持合并文本与旧实现逐字节一致（可用现有测试快照对比）。

### 阶段 1：正确性修复（约 1~2 天）

| 任务 | 涉及文件 | 说明 |
|---|---|---|
| 取消竞态修复 | `service.py:107-114` | `cancel_job` 持 `job_lock` 后 load→改→save；`should_cancel` 改读内存缓存 |
| JobStore 内存索引 + 细粒度锁 | `jobs.py` | 启动建索引；`_meta_locks` per-job；所有 load→save 路径持锁（save_upload/bind_feishu/append_log/on_log） |
| 日志 append-only | `jobs.py:167-172`、`service.py:154-160` | 日志写 `logs.jsonl`；meta 只存计数；`should_cancel` 不再读盘 |
| 飞书 token 缓存 | `feishu.py:30-31` | TTL 90min |
| 吞错点补日志 | `service.py:236-239`、`jobs.py:160-161`、`feishu.py:48-51` | 至少 warning 级记录 |
| 竞态回归测试 | `tests/` | 新增多线程取消/日志并发测试（阶段 1 的验收核心） |

**验收**：新增并发测试通过（运行中连续点终止 ×20 不丢标志；100 条日志并发写后 meta 无损坏）；坏 meta.json 的任务在 UI 上以"损坏"标签出现而非消失。
**回归风险**：中。JobStore 改造是服务端核心，需保持 HTTP API 响应结构不变（现有 `test_server.py` 26 条用例做护栏）。

### 阶段 2：知识源统一与 prompt 瘦身（约 2~3 天）

| 任务 | 涉及文件 | 说明 |
|---|---|---|
| 建 `knowledge/` | 新目录 | 迁移 templates/；新增 rules.yaml（先迁移"用例数区间、风险阈值、覆盖深度"等已矛盾项） |
| prompts 从 rules/schema 渲染 | `prompts.py` | 删除手写枚举与数值，改由 rules.yaml/schema.yaml 拼装；解决 S-02/S-03 矛盾 |
| skills_gen 生成 SKILL.md | 新 `scripts/skills_gen.py` | 3 个 SKILL.md 的规则段从 knowledge/ 生成；`install_skill.py` 安装时复制模板并校验哈希 |
| 批次 prompt 切片 | `prompts.py`、`runner.py` | 矩阵批只带本批 R + 相关节选；用例批带本批行 + 摘要；引入按字符预算的上下文裁剪（替换 `[:8000]` 魔数） |
| LLM 重试/退避/限流 | `llm.py` | 指数退避 ×3；全局 `BoundedSemaphore`；timeout 进配置 |

**验收**：grep 全仓库，用例数区间等技术数值只出现在 rules.yaml 一处；MockLLM 全流水线测试通过且 prompt 变短（记录 token 前后对比）；生成的 SKILL.md 与人工版 diff 仅在规则段；真实跑一次端到端对比产物质量不回退。
**回归风险**：中。prompt 内容变化直接影响生成质量，必须在真实需求上做 A/B 对比后再合入。

### 阶段 3：结构化中间产物与 Runner 状态机（约 3~5 天，核心重构）

| 任务 | 涉及文件 | 说明 |
|---|---|---|
| `flow/models.py` dataclass 全家 | 新 | RequirementItem/PlanItem/RiskItem/CoverageRow/TestCase 等 |
| `flow/render.py` 单向渲染 | 新 | dataclass → Markdown/YAML/xlsx 数据源；删除"解析自渲染文本"路径 |
| 逐步迁移：matrix → cases → review | `runner.py`、`parsing.py` | 每步 LLM 输出解析一次成对象；下游消费对象；`parsing.py` 预期从 801 行降至 ~350 行 |
| Step 注册表 + 状态机 | `flow/steps.py`、`runner.py` | `run()` 205 行拆解；进度结构化回调，删 `_STEP_RE` |
| 校验合并 | `cli.py:23-89`、`runner.py:377-435`、`tools.py` → `validate/` | 消除三处复制；tools 不再实例化 Runner |
| 断点恢复打通 | `pipeline.py`、`jobs.py` | `.qagent-pipeline.json` 步级续跑（不再要求 4 文件齐全才能续） |

**验收**：`parsing.py` 行数下降 ≥40%；"脚本补桩用例占比"这个质量指标可统计且较改造前下降（补桩由"解析失败兜底"变为"明确的补齐策略"）；CLI 各子命令输出不变；`tests/test_agent.py` 全流水线测试通过。
**回归风险**：高。这是行为敏感区，按"一个 Step 一个 PR"推进，每步保留旧路径开关（config `use_structured_flow: bool`）便于回退，双跑对比一个真实任务后再删旧路径。

### 阶段 4：服务层升级（约 3~5 天）

| 任务 | 涉及文件 | 说明 |
|---|---|---|
| SSE 事件推送 | `server/events.py`、`index.html` | 状态/日志/进度推送；前端 600ms 轮询仅保留列表页降频兜底 |
| 取消事件化 + 流式中断 | `service.py`、`llm.py` | `threading.Event`；流式 chunk 间检查取消 |
| 池分离与全局限流 | `service.py` | chat 池与 pipeline 池分离；LLM 全局信号量（阶段 2 已备）生效到服务层 |
| 飞书异步化（暂缓，暂不接入） | `app.py:147-155`、`feishu.py` | 先 ACK 后入队；删 nginx 3600s 补丁 |
| `cgi` 替换 | `app.py:5` | 手写 multipart 解析（`email.parser` 或 boundary 手撕），解锁 3.12+ |
| 重启恢复 | `service.py`、`store.py` | 启动扫描 running → 结合 pipeline 状态自动续跑或标 stale |
| chat 多轮工具循环 | `chat.py` | read_artifact 回流；结构化用例上下文；TypedDict 校验 action |
| 鉴权与隔离 | `auth.py`、`jobs.py` | owner 过滤落地；`X-User` 至少加服务端签发 |
| 前端拆分（可选） | `index.html` | 659 行单文件拆 CSS/JS 或引入轻量构建；此任务可延后 |

**验收**：取消延迟 ≤2s（流式下）；50 任务 × 3 浏览器场景下 `/api/jobs` P95 < 50ms（内存索引）；kill -9 服务进程后重启，running 任务自动恢复或明确标记；Python 3.12 镜像下 `qagent serve` 可用。（飞书 3s ACK 一项随接入一并暂缓）
**回归风险**：中高。前后端协议变化需同步发前端；SSE 在内网 nginx 下需配 `X-Accel-Buffering: no`。

### 优先级建议

若时间有限按此顺序取最大收益：**阶段 0 → 阶段 1（取消竞态 + 日志写放大）→ 阶段 2 的 prompt 瘦身与 LLM 重试（成本立降）→ 阶段 3（质量根源）→ 阶段 4**。P-05（token 放大）与 W-01（取消丢失）是用户可感知度最高的两项。

---

## 5. 风险与兼容性

| 使用入口 | 阶段 0-1 | 阶段 2 | 阶段 3 | 阶段 4 |
|---|---|---|---|---|
| Web（`qagent serve`） | 不变 | 不变（内部 prompt 变化影响生成质量，需 A/B） | 不变（断点续跑粒度变细，属增强） | 前端需同步更新（SSE）；旧浏览器降级轮询 |
| CLI（`qagent run/pipeline/...`） | 不变 | 不变 | 命令与输出不变 | 不变 |
| Cursor Skill（`/qa generate`） | 不变 | SKILL.md 改为生成物，触发方式不变；**手改 SKILL.md 的习惯需改为改 knowledge/** | 不变 | 不变 |
| Docker | setup 合并后镜像构建步骤简化 | 不变 | 不变 | nginx 配置需加 SSE 头、删 3600s 超时 |

其他风险：
- **数据兼容**：已有 `data/jobs/` 目录结构阶段 0-3 不变；阶段 4 日志迁移 logs.jsonl 时保留对旧 meta.logs 的读取兼容（启动时一次性迁移）。
- **prompt 质量回退**：阶段 2/3 都动了模型输入，务必固定 2~3 个真实需求作为回归集，每次改动后人工评审产物 + 对比补桩率。
- **双份模板过渡**：阶段 2 完成 skills_gen 前，先加 CI 检查（diff templates/ 与 skill 内副本，不一致即 fail），防止过渡期继续漂移。

---

## 6. 附录

### A. 问题索引总表

| 编号 | 严重度 | 摘要 | 关键证据 | 消解于 |
|---|---|---|---|---|
| S-01 | 🔴 | 模板 7 份 100% 重复 | templates/ vs skills/qa-orchestrator/templates/；config.py:224-235 | 阶段 2 |
| S-02 | 🔴 | 规则 4 处副本且矛盾（10~50 vs 15~40/30~80） | qa-testcase-generator/SKILL.md:53；prompts.py:200 | 阶段 2 |
| S-03 | 🟠 | 枚举契约漂移 | testcase.schema.yaml:1-2,26；chat.py SYSTEM；prompts.py:110-113 | 阶段 2 |
| S-04 | 🟠 | SKILL.md 重复行/三套步骤编号 | qa-orchestrator/SKILL.md:40-41；AGENT.md:44-56 | 阶段 0/2 |
| S-05 | 🟡 | 输入输出同名 | ingest.py:12-17 vs config.py:107-108 | 阶段 3（可选改名 scope.md） |
| W-01 | 🔴 | meta 读改写竞态、取消可被吞 | service.py:107-114 vs 122-186；jobs.py:114-172 | 阶段 1 |
| W-02 | 🔴 | 非原子写致任务消失 | jobs.py:114-119, 160-161 | 阶段 0/1 |
| W-03 | 🟠 | 重启无恢复、状态不打通 | pipeline.py:71-79 vs jobs.py:245-247 | 阶段 3/4 |
| W-04 | 🟠 | 进度靠日志正则 | service.py:25, 156-160 | 阶段 3 |
| W-05 | 🔴 | Markdown+正则耦合、39/110 补桩 | parsing.py:472,737；data/jobs/75abee1379c34d4a/meta.json:37 | 阶段 3 |
| W-06 | 🟠 | 修订上下文薄、read 不回流 | chat.py:120-139, 165-230 | 阶段 4 |
| W-07 | 🟠 | 取消粒度粗（600s 不可中断） | llm.py:50；runner.py:160-162 | 阶段 4 |
| W-08 | 🟠 | 飞书同步阻塞回调 | app.py:147-155；service.py:209-229；deploy/nginx.conf:16 | 阶段 4 |
| W-09 | 🟡 | 快照整目录复制不清理 | tools.py:207-221 | 阶段 4 |
| W-10 | 🟡 | owner 可伪造、任务全员共享 | auth.py；app.py:118 | 阶段 4 |
| P-01 | 🟠 | 600ms 轮询全量扫盘 | index.html:493-495；jobs.py:153-165；service.py:77-81 | 阶段 1/4 |
| P-02 | 🟠 | 每条日志全量读写 meta | jobs.py:167-172；service.py:154-163 | 阶段 1 |
| P-03 | 🟠 | PDF 解析 ×2、续跑重 ingest | ingest.py:128,144,170-171；service.py:142-147 | 阶段 0 |
| P-04 | 🟠 | LLM 无重试/池/流式；token 无缓存 | llm.py:24-59；feishu.py:30-31 | 阶段 2/4 |
| P-05 | 🟠 | 批次 prompt 全量上下文 | prompts.py:330-374；runner.py:272-313 | 阶段 2 |
| P-06 | 🟠 | 解析/配置零缓存、xlsx 导 2 次 | tools.py:39-52,161-204；app.py:24-32 | 阶段 0/3 |
| P-07 | 🟠 | 64 路并发无全局限流、池不分离 | service.py:55；runner.py:57-59 | 阶段 2/4 |
| P-08 | 🟡 | drawio 子树高度 O(n²) | mindmap.py:260-289 | 阶段 3（顺带） |
| Q-01 | 🔴 | serve.py 400 行死代码 | cli.py:322 | 阶段 0 |
| Q-02 | 🔴 | 校验三处复制 | cli.py:23-89；runner.py:377-435；tools.py:193-194 | 阶段 3 |
| Q-03 | 🟠 | run() 205 行等 | runner.py:437-641；cli.py:196-235 | 阶段 3 |
| Q-04 | 🟠 | 零 logging、吞错 | service.py:236-239；jobs.py:160-161；feishu.py:48-51 | 阶段 1 |
| Q-05 | 🔴 | import cgi + 双份打包 | app.py:5；setup.cfg:8-10 vs pyproject.toml:11-16 | 阶段 0/4 |
| Q-06 | 🟠 | 魔法数字、cwd 依赖 | runner.py:57-59；jobs.py:250-254；app.py:26 | 阶段 2/4 |
| Q-07 | 🟡 | 正则重复、兼容垫片 | runner.py:60；validation.py:15；scripts/qa_common.py | 阶段 0/3 |
| Q-08 | 🟡 | 并发/取消/续跑无测试 | tests/ | 各阶段随做随补 |

### B. 与既有计划文档的对齐

`docs/superpowers/plans/2026-08-20-qa-scope-clarify-control.md` 中作者已识别但未实施的改进（meta 原子写、`_meta_locks` 细粒度锁、`.cancel` 哨兵、stale 恢复）与本方案阶段 1/4 完全重合，实施时可直接引用其细节；本方案在其基础上补齐了知识源统一（阶段 2）与结构化产物（阶段 3）两条主线。

### C. 建议建立的度量基线

改造前后对比以下指标（当前值可在改造前跑 3 个真实需求采集）：
1. 端到端耗时与 LLM 调用次数、总 token 消耗（P-05 收益）；
2. `/api/jobs` 在 50 任务下的 P95 延迟（P-01/P-02 收益）；
3. 脚本补桩用例占比（W-05 收益，质量核心指标）；
4. 校验首轮通过率（阶段 3 收益）；
5. 取消请求生效延迟（W-07 收益）。
