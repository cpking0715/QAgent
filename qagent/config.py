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
    timeout: int = 600              # 单次请求超时（秒）
    retries: int = 3                # 429/5xx/网络错误的重试次数
    backoff_seconds: float = 1.0    # 重试退避基数（指数退避：1x/2x/4x）
    stream: bool = False            # 流式输出：可在 chunk 间响应取消（取消延迟秒级）

    def resolve_api_key(self, cli_override: str | None = None) -> str:
        """优先级：CLI > 配置文件 api_key > 环境变量。"""
        if cli_override:
            return cli_override
        if self.api_key:
            return self.api_key
        import os
        return os.environ.get(self.api_key_env, "")


def mask_api_key(key: str) -> str:
    text = (key or "").strip()
    if not text:
        return ""
    if len(text) <= 8:
        return text[:2] + "••••"
    return text[:3] + "••••" + text[-4:]


def local_yaml_path(workspace: Path | None = None) -> Path:
    return find_workspace_root(workspace) / "qagent.local.yaml"


def public_llm_settings(workspace: Path | None = None) -> dict[str, Any]:
    cfg = resolve_config(workspace=workspace)
    key = cfg.llm.resolve_api_key()
    source = ""
    if cfg.llm.api_key:
        source = "file"
    elif key:
        source = "env"
    return {
        "api_key_configured": bool(key),
        "api_key_hint": mask_api_key(key),
        "api_key_source": source,
        "model": cfg.llm.model,
        "base_url": cfg.llm.base_url,
    }


def update_local_llm(
    *,
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    workspace: Path | None = None,
) -> dict[str, Any]:
    """写入 qagent.local.yaml 的 llm 段，空字符串表示该项不改。"""
    path = local_yaml_path(workspace)
    data = _load_yaml(path)
    llm = data.get("llm")
    if not isinstance(llm, dict):
        llm = {}
    if api_key is not None and str(api_key).strip():
        llm["api_key"] = str(api_key).strip()
    if model is not None and str(model).strip():
        llm["model"] = str(model).strip()
    if base_url is not None and str(base_url).strip():
        llm["base_url"] = str(base_url).strip().rstrip("/")
    if not llm:
        raise ValueError("没有可保存的 LLM 设置")
    data["llm"] = llm
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return public_llm_settings(workspace or path.parent)


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
    # 批次 prompt 上下文模式：full=携带上游产物全文（现状）；sliced=按预算截断并按批过滤 R
    prompt_context_mode: str = "full"
    prompt_treq_budget: int = 6000
    prompt_plan_budget: int = 3000
    prompt_risk_budget: int = 3000

    @property
    def rules_path(self) -> Path:
        candidate = self.templates_dir / "rules.yaml"
        return candidate if candidate.is_file() else REPO_ROOT / "templates" / "rules.yaml"

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
    def test_requirements_xmind_path(self) -> Path:
        return self.output_dir / "test-requirements.xmind"

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
            timeout=int(llm_raw.get("timeout", 600)),
            retries=int(llm_raw.get("retries", 3)),
            backoff_seconds=float(llm_raw.get("backoff_seconds", 1.0)),
            stream=bool(llm_raw.get("stream", False)),
        )
    else:
        llm = LLMConfig()

    mode = str(merged.get("prompt_context_mode", "full"))
    if mode not in {"full", "sliced"}:
        mode = "full"
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
        prompt_context_mode=mode,
        prompt_treq_budget=int(merged.get("prompt_treq_budget", 6000)),
        prompt_plan_budget=int(merged.get("prompt_plan_budget", 3000)),
        prompt_risk_budget=int(merged.get("prompt_risk_budget", 3000)),
    )
