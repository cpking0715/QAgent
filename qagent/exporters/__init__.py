"""可插拔导出器。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from qagent.schema import TestcaseSchema


@dataclass
class ExportContext:
    output_path: Path
    schema: TestcaseSchema
    cases: list[dict]


class Exporter(Protocol):
    def export(self, ctx: ExportContext) -> Path: ...


def get_exporter(name: str) -> Exporter:
    if name == "xlsx":
        from qagent.exporters.xlsx import XlsxExporter
        return XlsxExporter()
    raise ValueError(f"未知导出器: {name}，可用: xlsx")
