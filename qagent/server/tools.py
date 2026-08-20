"""对话修订工具：只改当前 Job 产物，再校验导出。"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from qagent.agent.llm import MockLLM
from qagent.agent.runner import QAgentRunner
from qagent.config import QAgentConfig, resolve_config
from qagent.exporters import export_cases_xlsx
from qagent.exporters.mindmap import write_requirements_drawio
from qagent.parsing import (
    fill_missing_cases,
    merge_cases,
    normalize_case,
    parse_cases,
    parse_coverage_matrix,
    parse_requirement_ids,
    parse_requirement_items,
    render_qa_review_md,
    render_testcases_md,
)
from qagent.schema import load_schema
from qagent.server.jobs import JobStore
from qagent.server.scope import line_matches_query


def _write_cases_and_xlsx(config: QAgentConfig, cases: list[dict]) -> None:
    config.testcases_path.write_text(render_testcases_md(cases), encoding="utf-8")
    export_cases_xlsx(
        config.testcases_xlsx_path,
        load_schema(config.schema_path),
        cases,
    )


def job_config(store: JobStore, job_id: str) -> QAgentConfig:
    base = resolve_config()
    return QAgentConfig(
        workspace=store.job_dir(job_id),
        input_dir=store.input_dir(job_id),
        output_dir=store.output_dir(job_id),
        language=base.language,
        schema_path=base.schema_path,
        templates_dir=base.templates_dir,
        retry_limit=base.retry_limit,
        strict_coverage=base.strict_coverage,
        skill_root=base.skill_root,
        llm=base.llm,
    )


def read_artifact(store: JobStore, job_id: str, name: str, query: str = "") -> str:
    aliases = {
        "plan": "test-plan.md",
        "test-plan": "test-plan.md",
        "cases": "testcases.md",
        "testcases": "testcases.md",
        "matrix": "coverage-matrix.md",
        "risk": "risk.md",
        "review": "qa-review.md",
        "requirements": "test-requirements.md",
    }
    filename = aliases.get(name, name)
    path = store.output_dir(job_id) / Path(filename).name
    if not path.is_file():
        return f"产物不存在: {filename}"
    text = path.read_text(encoding="utf-8")
    if query:
        hits = [
            line for line in text.splitlines()
            if line_matches_query(line, query)
        ]
        if not hits:
            return f"{filename} 中未找到 {query!r}"
        return "\n".join(hits[:40])
    if len(text) > 6000:
        return text[:6000] + "\n…(已截断)"
    return text


def patch_plan(
    store: JobStore,
    job_id: str,
    add: list[dict[str, str]] | None = None,
    edit: dict[str, str] | None = None,
) -> str:
    path = store.output_dir(job_id) / "test-plan.md"
    if not path.is_file():
        raise FileNotFoundError("还没有 test-plan.md")
    text = path.read_text(encoding="utf-8")
    match = re.search(r"```requirements\s*\n(.*)\n```", text, re.DOTALL)
    if not match:
        raise ValueError("test-plan.md 缺少 requirements 块")
    lines = match.group(1).splitlines()
    by_id: dict[str, str] = {}
    order: list[str] = []
    for line in lines:
        if ":" not in line or line.strip().startswith("#"):
            continue
        rid, desc = line.split(":", 1)
        rid = rid.strip()
        if rid:
            order.append(rid)
            by_id[rid] = desc.strip()
    for item in add or []:
        rid = str(item.get("id") or "").strip()
        desc = str(item.get("text") or item.get("desc") or "").strip()
        if not rid or not desc:
            continue
        if rid not in by_id:
            order.append(rid)
        by_id[rid] = desc
    for rid, desc in (edit or {}).items():
        if rid in by_id and desc:
            by_id[rid] = desc
    block = "\n".join(f"{rid}: {by_id[rid]}" for rid in order if rid in by_id)
    new_text = text[: match.start(1)] + block + text[match.end(1) :]
    path.write_text(new_text, encoding="utf-8")
    return f"已更新需求条目 {len(order)} 条"


def upsert_cases(store: JobStore, job_id: str, incoming: list[dict]) -> str:
    config = job_config(store, job_id)
    existing: list[dict] = []
    if config.testcases_path.is_file():
        existing = parse_cases(config.testcases_path)
    req_items: list[tuple[str, str]] = []
    if config.test_plan_path.is_file():
        req_items = parse_requirement_items(config.test_plan_path)
    req_ids = {rid for rid, _ in req_items} or (
        parse_requirement_ids(config.test_plan_path) if config.test_plan_path.is_file() else set()
    )
    sc_to_req = {}
    if config.coverage_matrix_path.is_file():
        sc_to_req = {
            row.scenario_id: row.requirement_id
            for row in parse_coverage_matrix(config.coverage_matrix_path)
        }
    cleaned = [
        normalize_case(dict(case), req_ids, sc_to_req, req_items=req_items)
        for case in incoming
    ]
    merged = merge_cases(existing, cleaned)
    _write_cases_and_xlsx(config, merged)
    return f"用例已合并，当前 {len(merged)} 条"


def delete_cases(store: JobStore, job_id: str, ids: list[str]) -> str:
    config = job_config(store, job_id)
    if not config.testcases_path.is_file():
        raise FileNotFoundError("还没有 testcases.md")
    drop = {str(i) for i in ids}
    cases = [c for c in parse_cases(config.testcases_path) if str(c.get("id")) not in drop]
    _write_cases_and_xlsx(config, cases)
    return f"已删除 {sorted(drop)}，剩余 {len(cases)} 条"


def validate_and_export(store: JobStore, job_id: str, fill_gaps: bool = True) -> dict:
    config = job_config(store, job_id)
    if fill_gaps and config.testcases_path.is_file() and config.coverage_matrix_path.is_file():
        rows = parse_coverage_matrix(config.coverage_matrix_path)
        cases = parse_cases(config.testcases_path)
        req_items = parse_requirement_items(config.test_plan_path)
        req_ids = {rid for rid, _ in req_items}
        sc_to_req = {row.scenario_id: row.requirement_id for row in rows}
        for case in cases:
            normalize_case(case, req_ids, sc_to_req, req_items=req_items)
        cases = fill_missing_cases(cases, rows)
        for case in cases:
            normalize_case(case, req_ids, sc_to_req, req_items=req_items)
        _write_cases_and_xlsx(config, cases)
    if config.test_requirements_path.is_file():
        write_requirements_drawio(
            config.test_requirements_path,
            config.test_requirements_drawio_path,
        )
        from qagent.exporters.mindmap import write_requirements_xmind

        write_requirements_xmind(
            config.test_requirements_path,
            config.test_requirements_xmind_path,
        )
    warnings: list[str] = []
    if config.testcases_path.is_file() and config.coverage_matrix_path.is_file():
        cases = parse_cases(config.testcases_path)
        rows = parse_coverage_matrix(config.coverage_matrix_path)
        config.qa_review_path.write_text(
            render_qa_review_md(rows, cases), encoding="utf-8",
        )
        runner = QAgentRunner(config, MockLLM({}))
        errors, warnings = runner._full_validate()
        export_cases_xlsx(
            config.testcases_xlsx_path,
            load_schema(config.schema_path),
            cases,
        )
        if errors:
            store.refresh_artifacts(job_id)
            return {"ok": False, "errors": errors, "warnings": warnings}
    store.refresh_artifacts(job_id)
    return {"ok": True, "errors": [], "warnings": warnings}


def snapshot_output(store: JobStore, job_id: str) -> Path:
    src = store.output_dir(job_id)
    snap = src / ".snapshot"
    if snap.exists():
        shutil.rmtree(snap)
    snap.mkdir(parents=True)
    for item in src.iterdir():
        if item.name.startswith("."):
            continue
        dest = snap / item.name
        if item.is_file():
            shutil.copy2(item, dest)
        elif item.is_dir():
            shutil.copytree(item, dest)
    return snap


def restore_snapshot(store: JobStore, job_id: str) -> None:
    src = store.output_dir(job_id)
    snap = src / ".snapshot"
    if not snap.is_dir():
        return
    for item in src.iterdir():
        if item.name.startswith("."):
            continue
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()
    for item in snap.iterdir():
        dest = src / item.name
        if item.is_file():
            shutil.copy2(item, dest)
        else:
            shutil.copytree(item, dest)
