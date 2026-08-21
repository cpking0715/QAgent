"""QAgent 独立小 Agent 运行器。"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from qagent.agent.llm import LLMClient
from qagent.agent.prompts import (
    build_coverage_matrix_prompt,
    build_fix_matrix_prompt,
    build_fix_prompt,
    build_risk_prompt,
    build_test_plan_prompt,
    build_test_requirements_prompt,
    build_testcases_prompt,
    extract_document,
)
from qagent.config import QAgentConfig
from qagent.exporters import export_cases_xlsx
from qagent.parsing import (
    SC_ID_RE,
    CoverageRow,
    merge_cases,
    parse_cases,
    parse_cases_text,
    fill_missing_cases,
    ensure_requirements_have_cases,
    normalize_case,
    parse_coverage_matrix,
    parse_coverage_matrix_text,
    parse_requirement_ids,
    parse_requirement_items,
    parse_review_trace,
    parse_risks,
    finalize_matrix_rows,
    render_coverage_matrix_md,
    render_coverage_table,
    render_qa_review_md,
    render_testcases_md,
)
from qagent.pipeline import STEP_ORDER, PipelineStep, init_pipeline, mark_step
from qagent.schema import load_schema
from qagent.validation import (
    full_validate,
    validate_matrix,
)

CASE_BATCH_SIZE = 12
MATRIX_REQ_BATCH = 16
MAX_WORKERS = 8


def _chunks(items: list, size: int) -> list:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _sc_ids_from_errors(errors: list[str]) -> set[str]:
    found: set[str] = set()
    for message in errors:
        found.update(SC_ID_RE.findall(message))
    return found


def _uncovered_matrix_rows(
    rows: list[CoverageRow], cases: list[dict],
) -> list[CoverageRow]:
    have: set[str] = set()
    for case in cases:
        blob = f"{case.get('id', '')} {case.get('title', '')}"
        have.update(SC_ID_RE.findall(str(blob)))
    if not have:
        return rows
    return [row for row in rows if row.scenario_id not in have]


def _case_scenario_ids(case: dict) -> list[str]:
    blob = f"{case.get('id', '')} {case.get('title', '')}"
    return SC_ID_RE.findall(str(blob))


def keep_one_case_per_row(
    cases: list[dict], rows: list[CoverageRow],
) -> list[dict]:
    """每个矩阵行只留 1 条用例；优先用 title 里的 SC，否则按 R 兜底。"""
    wanted = [row.scenario_id for row in rows]
    wanted_set = set(wanted)
    by_req = {row.scenario_id: row.requirement_id for row in rows}
    picked: dict[str, dict] = {}
    unused: list[dict] = []
    for case in cases:
        assigned = next(
            (sid for sid in _case_scenario_ids(case) if sid in wanted_set and sid not in picked),
            None,
        )
        if assigned:
            picked[assigned] = case
        else:
            unused.append(case)
    for sid in wanted:
        if sid in picked:
            continue
        rid = by_req[sid]
        for index, case in enumerate(unused):
            refs = [
                part.strip()
                for part in str(case.get("requirement_ref") or "").split(",")
                if part.strip()
            ]
            if rid in refs:
                title = str(case.get("title") or "")
                if sid not in title:
                    case["title"] = f"{sid} {title}".strip()
                picked[sid] = case
                unused.pop(index)
                break
    return [picked[sid] for sid in wanted if sid in picked]


# 分段工作流的合法停止点（执行完该步骤后返回，等人工确认/修改产物再继续）
STOP_POINTS = ("test_requirements", "test_plan", "risk", "coverage_matrix", "testcases")


class JobCancelled(Exception):
    """用户请求终止当前流水线。"""


@dataclass
class RunResult:
    success: bool
    requirement_path: Path
    output_dir: Path
    artifacts: dict[str, Path] = field(default_factory=dict)
    case_count: int = 0
    errors: list[str] = field(default_factory=list)
    steps_completed: list[str] = field(default_factory=list)
    stopped_after: str | None = None  # 分段模式：本次执行到该步骤后停下


# 流水线步骤间的共享状态（状态机各步骤方法读写）
@dataclass
class _FlowState:
    treq: str = ""
    plan: str = ""
    risk: str = ""
    matrix: str = ""
    matrix_rows: list = field(default_factory=list)
    cases: list = field(default_factory=list)
    existing_cases: list = field(default_factory=list)
    cases_content: str = ""
    review_content: str = ""


# 续跑：上游产物 key → _FlowState 属性名
_STATE_ATTR = {
    "test_requirements": "treq",
    "test_plan": "plan",
    "risk": "risk",
    "coverage_matrix": "matrix",
}
_RESUME_KEYS = ["test_requirements", "test_plan", "risk", "coverage_matrix"]
_START_CHOICES = set(_STATE_ATTR) | {"requirements", "auto"}
# 续跑执行顺序：起点之后（含起点）的生成步骤依次执行
_EXEC_ORDER = ["test_requirements", "test_plan", "risk", "coverage_matrix", "testcases"]


class QAgentRunner:
    """独立小 Agent：LLM 生成 Step 2-7，脚本校验 Step 8，导出 Step 9。"""

    def __init__(
        self,
        config: QAgentConfig,
        llm: LLMClient,
        on_log: Callable[[str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        on_step: Callable[[str, int, int, str], None] | None = None,
    ) -> None:
        self.config = config
        self.llm = llm
        self.schema = load_schema(config.schema_path)
        self._on_log = on_log
        self._should_cancel = should_cancel or (lambda: False)
        self._on_step = on_step

    def _stepno(self, step: PipelineStep) -> int:
        return STEP_ORDER.index(step) + 1

    def _notify_step(self, step: PipelineStep) -> None:
        """结构化进度上报（step_id / 序号 / 总数 / 标签），供服务层直接展示。"""
        if self._on_step:
            self._on_step(step.value, self._stepno(step), len(STEP_ORDER), step.value)

    def _check_cancel(self) -> None:
        if self._should_cancel():
            raise JobCancelled("用户终止")

    def _log(self, message: str) -> None:
        self._check_cancel()
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[QAgent {ts}] {message}", flush=True)
        if self._on_log:
            self._on_log(message)

    def _write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _export_mindmap(self) -> None:
        # drawio 已不再自动输出（文件用本地默认应用打开）；需要时 qagent mindmap 手动转
        try:
            from qagent.exporters.mindmap import write_requirements_xmind

            write_requirements_xmind(
                self.config.test_requirements_path,
                self.config.test_requirements_xmind_path,
            )
            if self.config.test_requirements_xmind_path.is_file():
                self._log(f"已写出 {self.config.test_requirements_xmind_path.name}")
        except (OSError, ValueError) as exc:
            self._log(f"WARNING: test-requirements.xmind 未生成: {exc}")

    def _finalize_cases(
        self, cases: list[dict], matrix_rows: list[CoverageRow],
    ) -> list[dict]:
        req_items = parse_requirement_items(self.config.test_plan_path)
        req_ids = {rid for rid, _ in req_items} or parse_requirement_ids(self.config.test_plan_path)
        sc_to_req = {row.scenario_id: row.requirement_id for row in matrix_rows}
        before = len(cases)
        for case in cases:
            normalize_case(case, req_ids, sc_to_req, req_items=req_items)
        cases = fill_missing_cases(cases, matrix_rows)
        cases = ensure_requirements_have_cases(cases, matrix_rows, req_ids)
        for case in cases:
            normalize_case(case, req_ids, sc_to_req, req_items=req_items)
        added = len(cases) - before
        if added > 0:
            self._log(f"  脚本补齐 {added} 条用例（批次缺失或需求未挂上）")
        return cases

    def _write_cases(self, cases: list[dict]) -> str:
        content = render_testcases_md(cases)
        self._write(self.config.testcases_path, content)
        export_cases_xlsx(self.config.testcases_xlsx_path, self.schema, cases)
        return content

    def _write_review(self, matrix_rows: list[CoverageRow], cases: list[dict]) -> str:
        content = render_qa_review_md(matrix_rows, cases)
        self._write(self.config.qa_review_path, content)
        return content

    def _generate_matrix_batches(
        self,
        treq_content: str,
        plan_content: str,
        risk_content: str,
    ) -> str:
        items = parse_requirement_items(self.config.test_plan_path)
        req_ids = [rid for rid, _ in items]
        chunks = list(_chunks(req_ids, MATRIX_REQ_BATCH)) or [req_ids]
        total = len(chunks)
        workers = min(MAX_WORKERS, total)
        self._log(f"  覆盖矩阵按需求分 {total} 批，并行 {workers} 路")

        def work(index: int, reqs: list[str]) -> tuple[int, list[CoverageRow]]:
            self._check_cancel()
            sys_prompt, user_prompt = build_coverage_matrix_prompt(
                treq_content, plan_content, risk_content, self.config,
                requirement_ids=reqs,
            )
            raw = extract_document(self.llm.complete(sys_prompt, user_prompt))
            try:
                return index, parse_coverage_matrix_text(raw)
            except ValueError as exc:
                self._log(f"  矩阵批次 {index}/{total} 解析失败: {exc}")
                return index, []

        by_index: dict[int, list[CoverageRow]] = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(work, index, chunk)
                for index, chunk in enumerate(chunks, 1)
            ]
            for future in as_completed(futures):
                self._check_cancel()
                index, rows = future.result()
                by_index[index] = rows
                self._log(f"  矩阵批次 {index}/{total} 解析到 {len(rows)} 行")

        merged: list[CoverageRow] = []
        for index in range(1, total + 1):
            merged.extend(by_index.get(index) or [])
        before = len(merged)
        finalized = finalize_matrix_rows(merged, items)
        self._log(f"  矩阵合并 {before} 行 → 校验后 {len(finalized)} 行（含补齐缺失需求）")
        return render_coverage_matrix_md(finalized)

    def _generate_case_batches(
        self,
        rows: list[CoverageRow],
        treq_content: str,
        plan_content: str,
        risk_content: str,
    ) -> list[dict]:
        if not rows:
            return []
        chunks = list(_chunks(rows, CASE_BATCH_SIZE))
        total = len(chunks)
        workers = min(MAX_WORKERS, total)
        self._log(f"  用例 {total} 批并行 {workers} 路，每批最多 {CASE_BATCH_SIZE} 行")

        def work(index: int, chunk: list[CoverageRow]) -> tuple[int, list[dict]]:
            self._check_cancel()
            matrix_slice = render_coverage_table(chunk)
            batch_req_ids = sorted({row.requirement_id for row in chunk})
            sys_prompt, user_prompt = build_testcases_prompt(
                treq_content, plan_content, risk_content, matrix_slice, self.config,
                requirement_ids=batch_req_ids,
            )
            raw = extract_document(self.llm.complete(sys_prompt, user_prompt))
            try:
                incoming = parse_cases_text(raw)
            except ValueError as exc:
                self._log(f"  批次 {index} 解析失败: {exc}")
                incoming = []
            kept = keep_one_case_per_row(incoming, chunk)
            kept = fill_missing_cases(kept, chunk)
            return index, kept

        cases: list[dict] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(work, index, chunk)
                for index, chunk in enumerate(chunks, 1)
            ]
            for future in as_completed(futures):
                self._check_cancel()
                index, incoming = future.result()
                self._log(f"  批次 {index}/{total} 保留 {len(incoming)} 条（一行一条）")
                cases = merge_cases(cases, incoming)
        return cases

    # 步级续跑：起点 → 需要从磁盘复用的上游产物
    _UPSTREAM_NEEDS = {
        "test_requirements": [],
        "test_plan": ["test_requirements"],
        "risk": ["test_requirements", "test_plan"],
        "coverage_matrix": ["test_requirements", "test_plan", "risk"],
        "testcases": ["test_requirements", "test_plan", "risk", "coverage_matrix"],
    }
    _UPSTREAM_PATHS = {
        "test_requirements": ("测试需求", lambda c: c.test_requirements_path, PipelineStep.TEST_REQUIREMENTS),
        "test_plan": ("测试方案", lambda c: c.test_plan_path, PipelineStep.TEST_PLAN),
        "risk": ("风险", lambda c: c.risk_path, PipelineStep.RISK),
        "coverage_matrix": ("覆盖矩阵", lambda c: c.coverage_matrix_path, PipelineStep.COVERAGE_MATRIX),
    }

    def _resolve_start(self, start_from: str) -> str | None:
        """把 start_from 归一化为具体步骤名；requirements 返回 None 表示全跑。"""
        if start_from == "requirements":
            return None
        if start_from == "auto":
            for key in _RESUME_KEYS:
                if not self._UPSTREAM_PATHS[key][1](self.config).is_file():
                    return key
            return "testcases"
        if start_from not in self._UPSTREAM_NEEDS:
            return None
        return start_from

    def _load_upstream(self, start: str) -> "_FlowState | list[str]":
        """按起点加载需复用的上游产物；失败返回错误列表。"""
        state = _FlowState()
        needs = self._UPSTREAM_NEEDS[start]
        missing = [
            f"续跑缺少产物 {self._UPSTREAM_PATHS[key][0]}: {self._UPSTREAM_PATHS[key][1](self.config)}"
            for key in needs
            if not self._UPSTREAM_PATHS[key][1](self.config).is_file()
        ]
        if missing:
            return missing
        for key in needs:
            path = self._UPSTREAM_PATHS[key][1](self.config)
            content = path.read_text(encoding="utf-8")
            setattr(state, _STATE_ATTR[key], content)
        if "coverage_matrix" in needs:
            try:
                state.matrix_rows = parse_coverage_matrix(self.config.coverage_matrix_path)
            except ValueError as exc:
                return [str(exc)]
        return state

    def _repair_cases(
        self,
        cases: list[dict],
        errors: list[str],
        matrix_rows: list[CoverageRow],
        treq_content: str,
        plan_content: str,
        risk_content: str,
        cases_content: str,
        matrix_content: str,
        review_content: str,
    ) -> list[dict]:
        missing = _sc_ids_from_errors(errors)
        if not cases:
            return self._generate_case_batches(
                matrix_rows, treq_content, plan_content, risk_content,
            )
        if missing:
            rows = [row for row in matrix_rows if row.scenario_id in missing]
            if rows:
                incoming = self._generate_case_batches(
                    rows, treq_content, plan_content, risk_content,
                )
                return merge_cases(cases, incoming)
        sys_prompt, user_prompt = build_fix_prompt(
            cases_content, errors, plan_content, self.config,
            test_requirements_text=treq_content,
            coverage_matrix_text=matrix_content,
            review_text=review_content,
        )
        raw = extract_document(self.llm.complete(sys_prompt, user_prompt))
        try:
            incoming = parse_cases_text(raw)
        except ValueError as exc:
            self._log(f"修正稿解析失败: {exc}")
            return cases
        if incoming:
            return merge_cases(cases, incoming)
        return cases

    def _full_validate(self) -> tuple[list[str], list[str]]:
        outcome = full_validate(self.config)
        return outcome.errors, outcome.warnings

    def run(
        self,
        requirement_path: Path,
        start_from: str = "requirements",
        stop_after: str | None = None,
    ) -> RunResult:
        requirement_path = requirement_path.resolve()
        requirement_text = requirement_path.read_text(encoding="utf-8")
        if stop_after is not None and stop_after not in STOP_POINTS:
            raise ValueError(f"无效停止点: {stop_after}（可选: {sorted(STOP_POINTS)}）")
        result = RunResult(
            success=False,
            requirement_path=requirement_path,
            output_dir=self.config.output_dir,
        )

        init_pipeline(self.config, requirement_path)
        self._log(f"源文档: {requirement_path.name} → {self.config.output_dir}")
        state = _FlowState()

        start = self._resolve_start(start_from)
        # auto（缺什么补什么）：已有产物直接复用，不重新生成也不覆盖
        auto_keep = start_from == "auto"

        def reached(checkpoint: str) -> bool:
            """checkpoint 步骤是否在本次执行范围内（含起点）。"""
            if start is None:
                return True
            return _EXEC_ORDER.index(checkpoint) >= _EXEC_ORDER.index(start)

        def reuse_existing(key: str) -> bool:
            """auto 模式：产物文件已存在则复用其内容并跳过该步生成。"""
            label, path_fn, step_enum = self._UPSTREAM_PATHS[key]
            path = path_fn(self.config)
            if not path.is_file():
                return False
            if key == "coverage_matrix":
                try:
                    state.matrix_rows = parse_coverage_matrix(path)
                except ValueError:
                    self._log(f"WARNING: 已有「{label}」无法解析，将重新生成")
                    return False
            setattr(state, _STATE_ATTR[key], path.read_text(encoding="utf-8"))
            mark_step(self.config, step_enum, requirement_path)
            result.steps_completed.append(key)
            self._log(f"「{label}」已存在，直接复用（不重新生成）")
            return True

        def halted(checkpoint: str) -> bool:
            """分段模式：checkpoint 执行完毕后是否应停下等待人工确认。"""
            if stop_after is None or checkpoint != stop_after:
                return False
            result.stopped_after = checkpoint
            result.success = True
            result.case_count = len(state.cases)
            result.artifacts = self._collect_artifacts()
            self._log(f"已生成至「{checkpoint}」，可修改产物后继续下一阶段")
            return True

        if start is not None:
            loaded = self._load_upstream(start)
            if isinstance(loaded, list):
                result.errors = loaded
                return result
            state = loaded
            reused = self._UPSTREAM_NEEDS[start]
            # 复用的产物补记 pipeline 状态（原实现只记 result，漏标前 4 步）
            for key in reused:
                mark_step(self.config, self._UPSTREAM_PATHS[key][2], requirement_path)
                result.steps_completed.append(key)
            if start == "testcases":
                self._log("续跑：复用已有测试需求/方案/风险/矩阵，从 Step 6 生成用例")
            elif reused:
                self._log(f"续跑：复用 {len(reused)} 份上游产物，从 {start} 继续")
            else:
                self._log("从测试需求开始生成")
            self._export_mindmap()
            if start == "testcases" and self.config.testcases_path.is_file():
                try:
                    state.existing_cases = parse_cases(self.config.testcases_path)
                    self._log(f"已有 {len(state.existing_cases)} 条用例，只补缺失场景")
                except ValueError as exc:
                    self._log(f"WARNING: 已有用例无法解析，将整批重生成: {exc}")

        if reached("test_requirements"):
            if not (auto_keep and reuse_existing("test_requirements")):
                self._step_requirements(state, requirement_text, requirement_path, result)
            if halted("test_requirements"):
                return result
        if reached("test_plan"):
            if not (auto_keep and reuse_existing("test_plan")):
                self._step_plan(state, requirement_text, result)
            if halted("test_plan"):
                return result
        if reached("risk"):
            if not (auto_keep and reuse_existing("risk")):
                self._step_risk(state, result)
            if halted("risk"):
                return result
        if reached("coverage_matrix"):
            if not (auto_keep and reuse_existing("coverage_matrix")):
                if not self._step_matrix(state, result):
                    return result
            if halted("coverage_matrix"):
                return result

        reuse_cases = False
        if auto_keep and self.config.testcases_path.is_file():
            try:
                existing = parse_cases(self.config.testcases_path)
            except ValueError as exc:
                self._log(f"WARNING: 已有用例无法解析，将重新生成: {exc}")
            else:
                if existing:
                    state.cases = existing
                    result.steps_completed.append("testcases")
                    self._log(f"用例已存在（{len(existing)} 条），复用并直接评审/校验/导出")
                    reuse_cases = True
        if not reuse_cases:
            self._step_testcases(state, result)
        if halted("testcases"):
            return result
        self._step_review(state, result)
        if not self._step_validate(state, result):
            return result
        self._step_export(state, result)

        result.case_count = len(state.cases)
        result.artifacts = self._collect_artifacts()
        result.success = True
        self._log(f"完成：{result.case_count} 条用例 → {self.config.output_dir}")
        return result

    def _collect_artifacts(self) -> dict[str, Path]:
        return {
            "test_requirements": self.config.test_requirements_path,
            "test_requirements_xmind": self.config.test_requirements_xmind_path,
            "test_plan": self.config.test_plan_path,
            "risk": self.config.risk_path,
            "coverage_matrix": self.config.coverage_matrix_path,
            "testcases": self.config.testcases_path,
            "qa_review": self.config.qa_review_path,
            "xlsx": self.config.testcases_xlsx_path,
        }

    # ---- 各步骤实现（状态机：读 state → 产出写回 state）----

    def _step_requirements(
        self, state: _FlowState, requirement_text: str,
        requirement_path: Path, result: RunResult,
    ) -> None:
        step = PipelineStep.TEST_REQUIREMENTS
        n = self._stepno(step)
        _total = len(STEP_ORDER)
        self._notify_step(step)
        self._log(
            f"Step {n}/{_total} 生成 test-requirements.md ..."
            "（分析 PRD+设计，约 1-3 分钟）",
        )
        t0 = time.perf_counter()
        sys_prompt, user_prompt = build_test_requirements_prompt(
            requirement_text, requirement_path, self.config,
        )
        state.treq = extract_document(self.llm.complete(sys_prompt, user_prompt))
        self._log(f"Step {n}/{_total} 完成，耗时 {time.perf_counter() - t0:.0f}s")
        self._write(self.config.test_requirements_path, state.treq)
        self._export_mindmap()
        mark_step(self.config, step, requirement_path)
        result.steps_completed.append("test_requirements")

    def _step_plan(
        self, state: _FlowState, requirement_text: str, result: RunResult,
    ) -> None:
        step = PipelineStep.TEST_PLAN
        n = self._stepno(step)
        _total = len(STEP_ORDER)
        self._notify_step(step)
        self._log(f"Step {n}/{_total} 生成 test-plan.md ...（等待 LLM）")
        t0 = time.perf_counter()
        sys_prompt, user_prompt = build_test_plan_prompt(
            state.treq, requirement_text, self.config,
        )
        state.plan = extract_document(self.llm.complete(sys_prompt, user_prompt))
        self._log(f"Step {n}/{_total} 完成，耗时 {time.perf_counter() - t0:.0f}s")
        self._write(self.config.test_plan_path, state.plan)
        mark_step(self.config, step)
        result.steps_completed.append("test_plan")

    def _step_risk(self, state: _FlowState, result: RunResult) -> None:
        step = PipelineStep.RISK
        n = self._stepno(step)
        _total = len(STEP_ORDER)
        self._notify_step(step)
        self._log(f"Step {n}/{_total} 生成 risk.md ...（等待 LLM）")
        t0 = time.perf_counter()
        sys_prompt, user_prompt = build_risk_prompt(state.treq, state.plan, self.config)
        state.risk = extract_document(self.llm.complete(sys_prompt, user_prompt))
        self._log(f"Step {n}/{_total} 完成，耗时 {time.perf_counter() - t0:.0f}s")
        self._write(self.config.risk_path, state.risk)
        mark_step(self.config, step)
        result.steps_completed.append("risk")

    def _step_matrix(self, state: _FlowState, result: RunResult) -> bool:
        step = PipelineStep.COVERAGE_MATRIX
        n = self._stepno(step)
        _total = len(STEP_ORDER)
        self._notify_step(step)
        self._log(f"Step {n}/{_total} 生成 coverage-matrix.md ...")
        t0 = time.perf_counter()
        state.matrix = self._generate_matrix_batches(state.treq, state.plan, state.risk)
        self._write(self.config.coverage_matrix_path, state.matrix)
        for attempt in range(1, self.config.retry_limit + 1):
            try:
                req_ids = parse_requirement_ids(self.config.test_plan_path)
                m_rows = parse_coverage_matrix(self.config.coverage_matrix_path)
                m_err, m_warn = validate_matrix(m_rows, req_ids, self.config)
            except ValueError as exc:
                m_err, m_warn = [str(exc)], []
            for w in m_warn:
                self._log(f"WARNING: {w}")
            if not m_err:
                break
            self._log(f"  覆盖矩阵校验未通过（第 {attempt}/{self.config.retry_limit} 次）: {m_err[0]}")
            if attempt >= self.config.retry_limit:
                result.errors = m_err
                return False
            sys_prompt, user_prompt = build_fix_matrix_prompt(
                state.matrix, m_err, state.plan, self.config,
            )
            raw_fix = extract_document(self.llm.complete(sys_prompt, user_prompt))
            try:
                fixed_rows = parse_coverage_matrix_text(raw_fix)
            except ValueError:
                fixed_rows = parse_coverage_matrix(self.config.coverage_matrix_path)
            items = parse_requirement_items(self.config.test_plan_path)
            state.matrix = render_coverage_matrix_md(finalize_matrix_rows(fixed_rows, items))
            self._write(self.config.coverage_matrix_path, state.matrix)
        self._log(f"Step {n}/{_total} 完成，耗时 {time.perf_counter() - t0:.0f}s")
        mark_step(self.config, step)
        result.steps_completed.append("coverage_matrix")
        # 结构化直传：复用校验循环中刚解析的行，不再从盘重读
        state.matrix_rows = m_rows
        return True

    def _step_testcases(self, state: _FlowState, result: RunResult) -> None:
        step = PipelineStep.TESTCASES
        n = self._stepno(step)
        _total = len(STEP_ORDER)
        self._notify_step(step)
        rows_to_gen = _uncovered_matrix_rows(state.matrix_rows, state.existing_cases)
        self._log(
            f"Step {n}/{_total} 生成 testcases.md "
            f"（按矩阵分批，待生成 {len(rows_to_gen)}/{len(state.matrix_rows)} 行，"
            f"每批 {CASE_BATCH_SIZE}）...",
        )
        t0 = time.perf_counter()
        incoming = self._generate_case_batches(
            rows_to_gen, state.treq, state.plan, state.risk,
        )
        state.cases = self._finalize_cases(
            merge_cases(state.existing_cases, incoming), state.matrix_rows,
        )
        state.cases_content = self._write_cases(state.cases)
        self._log(
            f"Step {n}/{_total} 完成，{len(state.cases)} 条用例，"
            f"耗时 {time.perf_counter() - t0:.0f}s",
        )
        mark_step(self.config, step)
        result.steps_completed.append("testcases")

    def _step_review(self, state: _FlowState, result: RunResult) -> None:
        step = PipelineStep.QA_REVIEW
        n = self._stepno(step)
        _total = len(STEP_ORDER)
        self._notify_step(step)
        self._log(f"Step {n}/{_total} 生成 qa-review.md ...")
        t0 = time.perf_counter()
        state.review_content = self._write_review(state.matrix_rows, state.cases)
        self._log(f"Step {n}/{_total} 完成，耗时 {time.perf_counter() - t0:.0f}s")
        mark_step(self.config, step)
        result.steps_completed.append("qa_review")

    def _step_validate(self, state: _FlowState, result: RunResult) -> bool:
        step = PipelineStep.VALIDATE
        n = self._stepno(step)
        _total = len(STEP_ORDER)
        self._notify_step(step)
        for attempt in range(1, self.config.retry_limit + 1):
            self._log(
                f"Step {n}/{_total} 校验（第 {attempt}/{self.config.retry_limit} 次）...",
            )
            errors, warnings = self._full_validate()
            for w in warnings:
                self._log(f"WARNING: {w}")
            if errors:
                self._log(f"校验失败 {len(errors)} 项，先脚本补齐")
                state.cases = self._finalize_cases(state.cases, state.matrix_rows)
                state.cases_content = self._write_cases(state.cases)
                state.review_content = self._write_review(state.matrix_rows, state.cases)
                errors, warnings = self._full_validate()
                for w in warnings:
                    self._log(f"WARNING: {w}")
            if not errors:
                mark_step(self.config, step)
                result.steps_completed.append("validate")
                return True
            self._log(f"脚本补齐后仍有 {len(errors)} 项：{errors[0]}")
            if attempt >= self.config.retry_limit:
                result.errors = errors
                result.case_count = len(state.cases)
                return False
            self._log("请求 LLM 修正 ...")
            t0 = time.perf_counter()
            state.cases = self._finalize_cases(
                self._repair_cases(
                    state.cases,
                    errors,
                    state.matrix_rows,
                    state.treq,
                    state.plan,
                    state.risk,
                    state.cases_content,
                    state.matrix,
                    state.review_content,
                ),
                state.matrix_rows,
            )
            state.cases_content = self._write_cases(state.cases)
            state.review_content = self._write_review(state.matrix_rows, state.cases)
            self._log(f"修正完成，耗时 {time.perf_counter() - t0:.0f}s")
        return True  # retry_limit<=0 时保持旧行为：未校验即通过

    def _step_export(self, state: _FlowState, result: RunResult) -> None:
        step = PipelineStep.EXPORT
        n = self._stepno(step)
        _total = len(STEP_ORDER)
        self._notify_step(step)
        self._log(f"Step {n}/{_total} 导出 testcases.xlsx ...")
        # state.cases 与 _write_cases 刚落盘的内容一致，直接导出
        export_cases_xlsx(self.config.testcases_xlsx_path, self.schema, state.cases)
        mark_step(self.config, step)
        result.steps_completed.append("export")
