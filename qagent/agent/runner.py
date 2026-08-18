"""QAgent 独立小 Agent 运行器。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from qagent.agent.llm import LLMClient
from qagent.agent.prompts import (
    build_coverage_matrix_prompt,
    build_fix_matrix_prompt,
    build_fix_prompt,
    build_qa_review_prompt,
    build_risk_prompt,
    build_test_plan_prompt,
    build_test_requirements_prompt,
    build_testcases_prompt,
    extract_document,
)
from qagent.config import QAgentConfig
from qagent.exporters import ExportContext, get_exporter
from qagent.parsing import (
    parse_cases,
    parse_coverage_matrix,
    parse_requirement_ids,
    parse_review_trace,
    parse_risks,
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

    def run(self, requirement_path: Path) -> RunResult:
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

        # Step 5: 覆盖矩阵
        self._log(f"Step 5/{total_steps} 生成 coverage-matrix.md ...")
        t0 = time.perf_counter()
        sys_prompt, user_prompt = build_coverage_matrix_prompt(
            treq_content, plan_content, risk_content, self.config,
        )
        matrix_content = extract_document(self.llm.complete(sys_prompt, user_prompt))
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
            if attempt >= self.config.retry_limit:
                result.errors = m_err
                return result
            sys_prompt, user_prompt = build_fix_matrix_prompt(
                matrix_content, m_err, plan_content, self.config,
            )
            matrix_content = extract_document(self.llm.complete(sys_prompt, user_prompt))
            self._write(self.config.coverage_matrix_path, matrix_content)
        self._log(f"Step 5/{total_steps} 完成，耗时 {time.perf_counter() - t0:.0f}s")
        mark_step(self.config, PipelineStep.COVERAGE_MATRIX)
        result.steps_completed.append("coverage_matrix")

        # Step 6: 测试用例
        self._log(
            f"Step 6/{total_steps} 生成 testcases.md ..."
            "（等待 LLM，约 2-5 分钟）",
        )
        t0 = time.perf_counter()
        sys_prompt, user_prompt = build_testcases_prompt(
            treq_content, plan_content, risk_content, matrix_content, self.config,
        )
        cases_content = extract_document(self.llm.complete(sys_prompt, user_prompt))
        self._log(f"Step 6/{total_steps} 完成，耗时 {time.perf_counter() - t0:.0f}s")
        self._write(self.config.testcases_path, cases_content)
        mark_step(self.config, PipelineStep.TESTCASES)
        result.steps_completed.append("testcases")

        # Step 7: QA Review
        self._log(f"Step 7/{total_steps} 生成 qa-review.md ...")
        t0 = time.perf_counter()
        sys_prompt, user_prompt = build_qa_review_prompt(
            matrix_content, cases_content, plan_content, risk_content, self.config,
        )
        review_content = extract_document(self.llm.complete(sys_prompt, user_prompt))
        self._log(f"Step 7/{total_steps} 完成，耗时 {time.perf_counter() - t0:.0f}s")
        self._write(self.config.qa_review_path, review_content)
        mark_step(self.config, PipelineStep.QA_REVIEW)
        result.steps_completed.append("qa_review")

        # Step 8: validate + fix loop
        plan_text = self.config.test_plan_path.read_text(encoding="utf-8")
        for attempt in range(1, self.config.retry_limit + 1):
            self._log(
                f"Step 8/{total_steps} 校验（第 {attempt}/{self.config.retry_limit} 次）...",
            )
            errors, warnings = self._full_validate()
            for w in warnings:
                self._log(f"WARNING: {w}")
            if not errors:
                mark_step(self.config, PipelineStep.VALIDATE)
                result.steps_completed.append("validate")
                break
            self._log(f"校验失败 {len(errors)} 项，请求 LLM 修正 ...")
            if attempt >= self.config.retry_limit:
                result.errors = errors
                return result
            t0 = time.perf_counter()
            sys_prompt, user_prompt = build_fix_prompt(
                cases_content, errors, plan_text, self.config,
                test_requirements_text=treq_content,
                coverage_matrix_text=matrix_content,
                review_text=review_content,
            )
            cases_content = extract_document(self.llm.complete(sys_prompt, user_prompt))
            self._write(self.config.testcases_path, cases_content)
            sys_prompt, user_prompt = build_qa_review_prompt(
                matrix_content, cases_content, plan_content, risk_content, self.config,
            )
            review_content = extract_document(self.llm.complete(sys_prompt, user_prompt))
            self._write(self.config.qa_review_path, review_content)
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
            "risk": self.config.risk_path,
            "coverage_matrix": self.config.coverage_matrix_path,
            "testcases": self.config.testcases_path,
            "qa_review": self.config.qa_review_path,
            "xlsx": self.config.testcases_xlsx_path,
        }
        result.success = True
        self._log(f"完成：{result.case_count} 条用例 → {self.config.output_dir}")
        return result
