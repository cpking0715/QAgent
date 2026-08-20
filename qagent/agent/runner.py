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
from qagent.exporters import ExportContext, get_exporter
from qagent.exporters.mindmap import write_test_plan_mindmaps
from qagent.parsing import (
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
from qagent.pipeline import PipelineStep, init_pipeline, mark_step
from qagent.schema import load_schema
from qagent.validation import (
    validate_cases,
    validate_matrix,
    validate_plan_structure,
    validate_review_trace,
    validate_risk_coverage,
)

CASE_BATCH_SIZE = 12
MATRIX_REQ_BATCH = 16
MAX_WORKERS = 8
_SC_ID_RE = re.compile(r"SC-\d{3}")


def _chunks(items: list, size: int) -> list:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _sc_ids_from_errors(errors: list[str]) -> set[str]:
    found: set[str] = set()
    for message in errors:
        found.update(_SC_ID_RE.findall(message))
    return found


def _uncovered_matrix_rows(
    rows: list[CoverageRow], cases: list[dict],
) -> list[CoverageRow]:
    have: set[str] = set()
    for case in cases:
        blob = f"{case.get('id', '')} {case.get('title', '')}"
        have.update(_SC_ID_RE.findall(str(blob)))
    if not have:
        return rows
    return [row for row in rows if row.scenario_id not in have]


def _case_scenario_ids(case: dict) -> list[str]:
    blob = f"{case.get('id', '')} {case.get('title', '')}"
    return _SC_ID_RE.findall(str(blob))


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


@dataclass
class RunResult:
    success: bool
    requirement_path: Path
    output_dir: Path
    artifacts: dict[str, Path] = field(default_factory=dict)
    case_count: int = 0
    errors: list[str] = field(default_factory=list)
    steps_completed: list[str] = field(default_factory=list)


class QAgentRunner:
    """独立小 Agent：LLM 生成 Step 2-7，脚本校验 Step 8，导出 Step 9。"""

    def __init__(
        self,
        config: QAgentConfig,
        llm: LLMClient,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.llm = llm
        self.schema = load_schema(config.schema_path)
        self._on_log = on_log

    def _log(self, message: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[QAgent {ts}] {message}", flush=True)
        if self._on_log:
            self._on_log(message)

    def _write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _export_mindmap(self) -> None:
        try:
            write_test_plan_mindmaps(
                plan_path=self.config.test_plan_path,
                md_path=self.config.test_plan_mindmap_md_path,
                mm_path=self.config.test_plan_mindmap_mm_path,
                matrix_path=(
                    self.config.coverage_matrix_path
                    if self.config.coverage_matrix_path.is_file()
                    else None
                ),
                risk_path=(
                    self.config.risk_path if self.config.risk_path.is_file() else None
                ),
                opml_path=self.config.test_plan_mindmap_opml_path,
            )
        except (OSError, ValueError) as exc:
            self._log(f"WARNING: 思维导图未生成: {exc}")

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
            matrix_slice = render_coverage_table(chunk)
            sys_prompt, user_prompt = build_testcases_prompt(
                treq_content, plan_content, risk_content, matrix_slice, self.config,
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
                index, incoming = future.result()
                self._log(f"  批次 {index}/{total} 保留 {len(incoming)} 条（一行一条）")
                cases = merge_cases(cases, incoming)
        return cases

    def _load_upstream_artifacts(self) -> tuple[str, str, str, str, list[CoverageRow]] | list[str]:
        needed = [
            ("测试需求", self.config.test_requirements_path),
            ("测试方案", self.config.test_plan_path),
            ("风险", self.config.risk_path),
            ("覆盖矩阵", self.config.coverage_matrix_path),
        ]
        missing = [f"{label}: {path}" for label, path in needed if not path.is_file()]
        if missing:
            return [f"续跑缺少产物 {item}" for item in missing]
        try:
            matrix_rows = parse_coverage_matrix(self.config.coverage_matrix_path)
        except ValueError as exc:
            return [str(exc)]
        return (
            self.config.test_requirements_path.read_text(encoding="utf-8"),
            self.config.test_plan_path.read_text(encoding="utf-8"),
            self.config.risk_path.read_text(encoding="utf-8"),
            self.config.coverage_matrix_path.read_text(encoding="utf-8"),
            matrix_rows,
        )

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
        errors: list[str] = []
        warnings: list[str] = []

        plan_path = self.config.test_plan_path
        cases_path = self.config.testcases_path
        risk_path = self.config.risk_path

        try:
            cases = parse_cases(cases_path)
        except ValueError as exc:
            return [str(exc)], warnings

        if plan_path.is_file():
            errors.extend(validate_plan_structure(plan_path, self.schema))

        requirement_ids = parse_requirement_ids(plan_path) if plan_path.is_file() else set()
        case_errors, case_warnings = validate_cases(
            cases, requirement_ids, self.schema, self.config,
        )
        errors.extend(case_errors)
        warnings.extend(case_warnings)

        if risk_path.is_file():
            try:
                risks = parse_risks(risk_path)
                risk_errors, risk_warnings = validate_risk_coverage(
                    cases, risks, self.schema,
                )
                errors.extend(risk_errors)
                warnings.extend(risk_warnings)
            except ValueError as exc:
                errors.append(str(exc))

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

        return errors, warnings

    def run(self, requirement_path: Path, start_from: str = "requirements") -> RunResult:
        requirement_path = requirement_path.resolve()
        requirement_text = requirement_path.read_text(encoding="utf-8")
        result = RunResult(
            success=False,
            requirement_path=requirement_path,
            output_dir=self.config.output_dir,
        )

        init_pipeline(self.config, requirement_path)
        self._log(f"源文档: {requirement_path.name} → {self.config.output_dir}")
        total_steps = 9
        existing_cases: list[dict] = []

        if start_from == "testcases":
            loaded = self._load_upstream_artifacts()
            if isinstance(loaded, list):
                result.errors = loaded
                return result
            treq_content, plan_content, risk_content, matrix_content, matrix_rows = loaded
            self._log("续跑：复用已有测试需求/方案/风险/矩阵，从 Step 6 生成用例")
            result.steps_completed.extend(
                ["test_requirements", "test_plan", "risk", "coverage_matrix"],
            )
            if self.config.testcases_path.is_file():
                try:
                    existing_cases = parse_cases(self.config.testcases_path)
                    self._log(f"已有 {len(existing_cases)} 条用例，只补缺失场景")
                except ValueError as exc:
                    self._log(f"WARNING: 已有用例无法解析，将整批重生成: {exc}")
        else:
            # Step 2: 测试需求（从 PRD + 设计文档）
            self._log(
                f"Step 2/{total_steps} 生成 test-requirements.md ..."
                "（分析 PRD+设计，约 1-3 分钟）",
            )
            t0 = time.perf_counter()
            sys_prompt, user_prompt = build_test_requirements_prompt(
                requirement_text, requirement_path, self.config,
            )
            treq_content = extract_document(self.llm.complete(sys_prompt, user_prompt))
            self._log(f"Step 2/{total_steps} 完成，耗时 {time.perf_counter() - t0:.0f}s")
            self._write(self.config.test_requirements_path, treq_content)
            mark_step(self.config, PipelineStep.TEST_REQUIREMENTS, requirement_path)
            result.steps_completed.append("test_requirements")

            # Step 3: 测试方案（基于测试需求）
            self._log(f"Step 3/{total_steps} 生成 test-plan.md ...（等待 LLM）")
            t0 = time.perf_counter()
            sys_prompt, user_prompt = build_test_plan_prompt(
                treq_content, requirement_text, self.config,
            )
            plan_content = extract_document(self.llm.complete(sys_prompt, user_prompt))
            self._log(f"Step 3/{total_steps} 完成，耗时 {time.perf_counter() - t0:.0f}s")
            self._write(self.config.test_plan_path, plan_content)
            self._export_mindmap()
            mark_step(self.config, PipelineStep.TEST_PLAN, requirement_path)
            result.steps_completed.append("test_plan")

            # Step 4: 风险分析
            self._log(f"Step 4/{total_steps} 生成 risk.md ...（等待 LLM）")
            t0 = time.perf_counter()
            sys_prompt, user_prompt = build_risk_prompt(
                treq_content, plan_content, self.config,
            )
            risk_content = extract_document(self.llm.complete(sys_prompt, user_prompt))
            self._log(f"Step 4/{total_steps} 完成，耗时 {time.perf_counter() - t0:.0f}s")
            self._write(self.config.risk_path, risk_content)
            mark_step(self.config, PipelineStep.RISK)
            result.steps_completed.append("risk")

            # Step 5: 覆盖矩阵（按需求分批并行）
            self._log(f"Step 5/{total_steps} 生成 coverage-matrix.md ...")
            t0 = time.perf_counter()
            matrix_content = self._generate_matrix_batches(
                treq_content, plan_content, risk_content,
            )
            self._write(self.config.coverage_matrix_path, matrix_content)
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
                    return result
                sys_prompt, user_prompt = build_fix_matrix_prompt(
                    matrix_content, m_err, plan_content, self.config,
                )
                raw_fix = extract_document(self.llm.complete(sys_prompt, user_prompt))
                try:
                    fixed_rows = parse_coverage_matrix_text(raw_fix)
                except ValueError:
                    fixed_rows = parse_coverage_matrix(self.config.coverage_matrix_path)
                items = parse_requirement_items(self.config.test_plan_path)
                matrix_content = render_coverage_matrix_md(finalize_matrix_rows(fixed_rows, items))
                self._write(self.config.coverage_matrix_path, matrix_content)
            self._log(f"Step 5/{total_steps} 完成，耗时 {time.perf_counter() - t0:.0f}s")
            mark_step(self.config, PipelineStep.COVERAGE_MATRIX)
            result.steps_completed.append("coverage_matrix")
            self._export_mindmap()
            matrix_rows = parse_coverage_matrix(self.config.coverage_matrix_path)

        # Step 6: 测试用例（按矩阵行分批，避免超长输出截断）
        rows_to_gen = _uncovered_matrix_rows(matrix_rows, existing_cases)
        self._log(
            f"Step 6/{total_steps} 生成 testcases.md "
            f"（按矩阵分批，待生成 {len(rows_to_gen)}/{len(matrix_rows)} 行，"
            f"每批 {CASE_BATCH_SIZE}）...",
        )
        t0 = time.perf_counter()
        incoming = self._generate_case_batches(
            rows_to_gen, treq_content, plan_content, risk_content,
        )
        cases = self._finalize_cases(merge_cases(existing_cases, incoming), matrix_rows)
        cases_content = self._write_cases(cases)
        self._log(
            f"Step 6/{total_steps} 完成，{len(cases)} 条用例，"
            f"耗时 {time.perf_counter() - t0:.0f}s",
        )
        mark_step(self.config, PipelineStep.TESTCASES)
        result.steps_completed.append("testcases")

        # Step 7: QA Review（追溯表由脚本按已有用例生成，不编造 ID）
        self._log(f"Step 7/{total_steps} 生成 qa-review.md ...")
        t0 = time.perf_counter()
        review_content = self._write_review(matrix_rows, cases)
        self._log(f"Step 7/{total_steps} 完成，耗时 {time.perf_counter() - t0:.0f}s")
        mark_step(self.config, PipelineStep.QA_REVIEW)
        result.steps_completed.append("qa_review")

        # Step 8: 先脚本补齐，再必要时让 LLM 修
        for attempt in range(1, self.config.retry_limit + 1):
            self._log(
                f"Step 8/{total_steps} 校验（第 {attempt}/{self.config.retry_limit} 次）...",
            )
            errors, warnings = self._full_validate()
            for w in warnings:
                self._log(f"WARNING: {w}")
            if errors:
                self._log(f"校验失败 {len(errors)} 项，先脚本补齐")
                cases = self._finalize_cases(cases, matrix_rows)
                cases_content = self._write_cases(cases)
                review_content = self._write_review(matrix_rows, cases)
                errors, warnings = self._full_validate()
                for w in warnings:
                    self._log(f"WARNING: {w}")
            if not errors:
                mark_step(self.config, PipelineStep.VALIDATE)
                result.steps_completed.append("validate")
                break
            self._log(f"脚本补齐后仍有 {len(errors)} 项：{errors[0]}")
            if attempt >= self.config.retry_limit:
                result.errors = errors
                return result
            self._log("请求 LLM 修正 ...")
            t0 = time.perf_counter()
            cases = self._finalize_cases(
                self._repair_cases(
                    cases,
                    errors,
                    matrix_rows,
                    treq_content,
                    plan_content,
                    risk_content,
                    cases_content,
                    matrix_content,
                    review_content,
                ),
                matrix_rows,
            )
            cases_content = self._write_cases(cases)
            review_content = self._write_review(matrix_rows, cases)
            self._log(f"修正完成，耗时 {time.perf_counter() - t0:.0f}s")

        # Step 9: export
        self._log(f"Step 9/{total_steps} 导出 testcases.xlsx ...")
        cases = parse_cases(self.config.testcases_path)
        exporter = get_exporter("xlsx")
        exporter.export(ExportContext(
            output_path=self.config.testcases_xlsx_path,
            schema=self.schema,
            cases=cases,
        ))
        mark_step(self.config, PipelineStep.EXPORT)
        result.steps_completed.append("export")

        result.case_count = len(cases)
        result.artifacts = {
            "test_requirements": self.config.test_requirements_path,
            "test_plan": self.config.test_plan_path,
            "test_plan_mindmap": self.config.test_plan_mindmap_md_path,
            "test_plan_mm": self.config.test_plan_mindmap_mm_path,
            "test_plan_opml": self.config.test_plan_mindmap_opml_path,
            "risk": self.config.risk_path,
            "coverage_matrix": self.config.coverage_matrix_path,
            "testcases": self.config.testcases_path,
            "qa_review": self.config.qa_review_path,
            "xlsx": self.config.testcases_xlsx_path,
        }
        result.success = True
        self._log(f"完成：{result.case_count} 条用例 → {self.config.output_dir}")
        return result
