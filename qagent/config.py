"""工作区与技能路径解析、qagent.yaml 加载。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent


@dataclass
class LLMConfig:
    model: str = "gpt-4o-mini"
    base_url: str = "https://api.openai.com/v1"
    api_key: str | None = None
    api_key_env: str = "OPENAI_API_KEY"
    temperature: float = 0.2
    max_tokens: int = 8192

    def resolve_api_key(self, cli_override: str | None = None) -> str:
        """优先级：CLI > 配置文件 api_key > 环境变量。"""
        if cli_override:
            return cli_override
        if self.api_key:
            return self.api_key
        import os
        return os.environ.get(self.api_key_env, "")


@dataclass
class QAgentConfig:
    workspace: Path
    input_dir: Path
    output_dir: Path
    language: str = "zh"
    schema_path: Path = field(default_factory=lambda: REPO_ROOT / "templates" / "testcase.schema.yaml")
    templates_dir: Path = field(default_factory=lambda: REPO_ROOT / "templates")
    retry_limit: int = 3
    strict_coverage: bool = False
    skill_root: Path | None = None
    llm: LLMConfig = field(default_factory=LLMConfig)

    @property
    def test_requirements_path(self) -> Path:
        return self.output_dir / "test-requirements.md"

    @property
    def test_plan_path(self) -> Path:
        return self.output_dir / "test-plan.md"

    @property
    def risk_path(self) -> Path:
        return self.output_dir / "risk.md"

    @property
    def coverage_matrix_path(self) -> Path:
        return self.output_dir / "coverage-matrix.md"

    @property
    def qa_review_path(self) -> Path:
        return self.output_dir / "qa-review.md"

    @property
    def testcases_path(self) -> Path:
        return self.output_dir / "testcases.md"

    @property
    def testcases_xlsx_path(self) -> Path:
        return self.output_dir / "testcases.xlsx"

    @property
    def test_plan_mindmap_md_path(self) -> Path:
        return self.output_dir / "test-plan-mindmap.md"

    @property
    def test_plan_mindmap_mm_path(self) -> Path:
        return self.output_dir / "test-plan.mm"

    @property
    def test_plan_mindmap_opml_path(self) -> Path:
        return self.output_dir / "test-plan.opml"

    @property
    def pipeline_state_path(self) -> Path:
        return self.output_dir / ".qagent-pipeline.json"


def find_workspace_root(start: Path | None = None) -> Path:
    """向上查找含 qagent.yaml 或 pyproject.toml 的目录。"""
    current = (start or Path.cwd()).resolve()
    if current.is_file():
        current = current.parent
    for directory in [current, *current.parents]:
        if (directory / "qagent.yaml").is_file():
            return directory
        if (directory / "pyproject.toml").is_file() and (directory / "qagent").is_dir():
            return directory
    return current


def find_skill_root(hint: Path | None = None) -> Path | None:
    """查找 qa-orchestrator 技能根目录。"""
    candidates: list[Path] = []
    if hint:
        candidates.append(hint.resolve())
        candidates.append(hint.resolve().parent)
    env_skill = Path.home() / ".cursor" / "skills" / "qa-orchestrator"
    candidates.extend([
        REPO_ROOT / "skills" / "qa-orchestrator",
        Path.cwd() / ".cursor" / "skills" / "qa-orchestrator",
        env_skill,
    ])
    seen: set[Path] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        if (path / "SKILL.md").is_file() and (path / "config.defaults.yaml").is_file():
            return path
    return None


def _merge_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if value is not None:
            merged[key] = value
    return merged


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def resolve_config(
    workspace: Path | None = None,
    skill_root: Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> QAgentConfig:
    """合并 defaults → skill defaults → qagent.yaml → CLI overrides。"""
    ws = find_workspace_root(workspace)
    skill = skill_root or find_skill_root()

    defaults: dict[str, Any] = {
        "input_dir": "input",
        "output_dir": "output",
        "language": "zh",
        "schema": "templates/testcase.schema.yaml",
        "retry_limit": 3,
        "strict_coverage": False,
    }
    if skill:
        defaults = _merge_config(defaults, _load_yaml(skill / "config.defaults.yaml"))

    project_cfg = _load_yaml(ws / "qagent.yaml")
    local_cfg = _load_yaml(ws / "qagent.local.yaml")
    merged = _merge_config(defaults, project_cfg)
    merged = _merge_config(merged, local_cfg)
    if overrides:
        merged = _merge_config(merged, overrides)

    def resolve_path(value: str, base: Path) -> Path:
        path = Path(value)
        if path.is_absolute():
            return path
        candidate = base / path
        if candidate.is_file() or candidate.is_dir():
            return candidate
        if skill and (skill / path).exists():
            return skill / path
        if (REPO_ROOT / path).exists():
            return REPO_ROOT / path
        return candidate

    schema_path = resolve_path(str(merged["schema"]), ws)
    templates_dir = schema_path.parent

    llm_raw = merged.get("llm") or merged.get("agent") or {}
    if isinstance(llm_raw, dict):
        llm = LLMConfig(
            model=str(llm_raw.get("model", "gpt-4o-mini")),
            base_url=str(llm_raw.get("base_url", "https://api.openai.com/v1")),
            api_key=str(llm_raw["api_key"]) if llm_raw.get("api_key") else None,
            api_key_env=str(llm_raw.get("api_key_env", "OPENAI_API_KEY")),
            temperature=float(llm_raw.get("temperature", 0.2)),
            max_tokens=int(llm_raw.get("max_tokens", 8192)),
        )
    else:
        llm = LLMConfig()

    return QAgentConfig(
        workspace=ws,
        input_dir=ws / str(merged["input_dir"]),
        output_dir=ws / str(merged["output_dir"]),
        language=str(merged.get("language", "zh")),
        schema_path=schema_path,
        templates_dir=templates_dir,
        retry_limit=int(merged.get("retry_limit", 3)),
        strict_coverage=bool(merged.get("strict_coverage", False)),
        skill_root=skill,
        llm=llm,
    )
