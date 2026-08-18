"""从 testcase.schema.yaml 加载校验与导出契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class FieldSpec:
    name: str
    required: bool = False
    type: str = "string"
    enum: set[str] = field(default_factory=set)
    pattern: re.Pattern[str] | None = None
    description: str = ""


@dataclass
class TestcaseSchema:
    version: int
    fields: dict[str, FieldSpec]
    list_fields: set[str]
    export_columns: list[tuple[str, str, int]]
    plan_required_sections: list[str]
    risk_zones: dict[str, dict[str, Any]]
    source_path: Path

    @property
    def required_fields(self) -> list[str]:
        return [name for name, spec in self.fields.items() if spec.required]

    @property
    def enum_fields(self) -> dict[str, set[str]]:
        return {
            name: spec.enum
            for name, spec in self.fields.items()
            if spec.enum
        }

    @property
    def id_pattern(self) -> re.Pattern[str] | None:
        spec = self.fields.get("id")
        return spec.pattern if spec else None


def load_schema(path: Path) -> TestcaseSchema:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Schema 文件格式无效: {path}")

    fields: dict[str, FieldSpec] = {}
    for name, spec in (raw.get("fields") or {}).items():
        if not isinstance(spec, dict):
            continue
        pattern = spec.get("pattern")
        fields[name] = FieldSpec(
            name=name,
            required=bool(spec.get("required", False)),
            type=str(spec.get("type", "string")),
            enum=set(spec.get("enum") or []),
            pattern=re.compile(pattern) if pattern else None,
            description=str(spec.get("description", "")),
        )

    export_columns: list[tuple[str, str, int]] = []
    for col in raw.get("export_columns") or []:
        export_columns.append((
            str(col["field"]),
            str(col["header"]),
            int(col.get("width", 20)),
        ))

    return TestcaseSchema(
        version=int(raw.get("version", 1)),
        fields=fields,
        list_fields=set(raw.get("list_fields") or []),
        export_columns=export_columns,
        plan_required_sections=list(raw.get("plan_required_sections") or []),
        risk_zones=dict(raw.get("risk_zones") or {}),
        source_path=path,
    )
