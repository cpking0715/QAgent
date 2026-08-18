"""流水线步骤状态机。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from qagent.config import QAgentConfig


class PipelineStep(str, Enum):
    PARSE_REQUIREMENT = "parse_requirement"
    TEST_REQUIREMENTS = "test_requirements"
    TEST_PLAN = "test_plan"
    RISK = "risk"
    COVERAGE_MATRIX = "coverage_matrix"
    TESTCASES = "testcases"
    QA_REVIEW = "qa_review"
    VALIDATE = "validate"
    EXPORT = "export"


STEP_ORDER = list(PipelineStep)


@dataclass
class PipelineState:
    current_step: str = PipelineStep.PARSE_REQUIREMENT.value
    completed: list[str] | None = None
    requirement_path: str | None = None
    updated_at: str | None = None

    def __post_init__(self) -> None:
        if self.completed is None:
            self.completed = []


def _artifact_for_step(step: PipelineStep, config: QAgentConfig) -> Path | None:
    mapping = {
        PipelineStep.TEST_REQUIREMENTS: config.test_requirements_path,
        PipelineStep.TEST_PLAN: config.test_plan_path,
        PipelineStep.RISK: config.risk_path,
        PipelineStep.COVERAGE_MATRIX: config.coverage_matrix_path,
        PipelineStep.TESTCASES: config.testcases_path,
        PipelineStep.QA_REVIEW: config.qa_review_path,
        PipelineStep.EXPORT: config.testcases_xlsx_path,
    }
    return mapping.get(step)


def load_state(config: QAgentConfig) -> PipelineState:
    path = config.pipeline_state_path
    if not path.is_file():
        return PipelineState()
    data = json.loads(path.read_text(encoding="utf-8"))
    return PipelineState(**data)


def save_state(config: QAgentConfig, state: PipelineState) -> None:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    state.updated_at = datetime.now(timezone.utc).isoformat()
    config.pipeline_state_path.write_text(
        json.dumps(asdict(state), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def mark_step(config: QAgentConfig, step: PipelineStep, requirement_path: Path | None = None) -> PipelineState:
    state = load_state(config)
    if step.value not in state.completed:
        state.completed.append(step.value)
    state.current_step = step.value
    if requirement_path:
        state.requirement_path = str(requirement_path)
    save_state(config, state)
    return state


def check_prerequisites(config: QAgentConfig, step: PipelineStep) -> list[str]:
    """返回阻止执行该步骤的错误列表。"""
    errors: list[str] = []
    step_index = STEP_ORDER.index(step)

    for prior in STEP_ORDER[:step_index]:
        if prior in (PipelineStep.PARSE_REQUIREMENT, PipelineStep.VALIDATE):
            continue
        artifact = _artifact_for_step(prior, config)
        if artifact and not artifact.is_file():
            errors.append(f"缺少前置产物: {artifact}（需先完成 {prior.value}）")

    if step == PipelineStep.VALIDATE:
        for path in (
            config.test_requirements_path,
            config.test_plan_path,
            config.coverage_matrix_path,
            config.testcases_path,
            config.qa_review_path,
        ):
            if not path.is_file():
                errors.append(f"校验缺少文件: {path}")
    if step == PipelineStep.EXPORT:
        for path in (
            config.test_requirements_path,
            config.test_plan_path,
            config.coverage_matrix_path,
            config.testcases_path,
            config.qa_review_path,
        ):
            if not path.is_file():
                errors.append(f"导出缺少文件: {path}")

    return errors


def init_pipeline(config: QAgentConfig, requirement_path: Path) -> PipelineState:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    state = PipelineState(
        requirement_path=str(requirement_path),
        completed=[],
        current_step=PipelineStep.PARSE_REQUIREMENT.value,
    )
    save_state(config, state)
    return state


def pipeline_status(config: QAgentConfig) -> dict:
    state = load_state(config)
    status: dict[str, dict] = {}
    for step in STEP_ORDER:
        artifact = _artifact_for_step(step, config)
        status[step.value] = {
            "completed": step.value in (state.completed or []),
            "artifact": str(artifact) if artifact else None,
            "artifact_exists": artifact.is_file() if artifact else None,
        }
    return {"state": asdict(state), "steps": status}
