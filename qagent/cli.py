"""qagent 命令行入口。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from qagent.config import LLMConfig, QAgentConfig, resolve_config
from qagent.exporters import ExportContext, get_exporter
from qagent.parsing import parse_cases, parse_requirement_ids, parse_risks
from qagent.pipeline import (
    PipelineStep,
    check_prerequisites,
    init_pipeline,
    mark_step,
    pipeline_status,
)
from qagent.schema import load_schema
from qagent.validation import validate_cases, validate_plan_structure, validate_risk_coverage


def _run_validate(config, cases_path: Path, plan_path: Path, risk_path: Path | None) -> int:
    schema = load_schema(config.schema_path)
    try:
        cases = parse_cases(cases_path)
        requirement_ids = parse_requirement_ids(plan_path)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1

    errors: list[str] = []
    warnings: list[str] = []

    if plan_path.is_file():
        errors.extend(validate_plan_structure(plan_path, schema))

    case_errors, case_warnings = validate_cases(cases, requirement_ids, schema, config)
    errors.extend(case_errors)
    warnings.extend(case_warnings)

    if risk_path and risk_path.is_file():
        try:
            risks = parse_risks(risk_path)
            risk_errors, risk_warnings = validate_risk_coverage(cases, risks, schema)
            errors.extend(risk_errors)
            warnings.extend(risk_warnings)
        except (OSError, ValueError) as exc:
            errors.append(f"risk.md 解析失败: {exc}")

    from qagent.parsing import parse_coverage_matrix, parse_review_trace
    from qagent.validation import validate_matrix, validate_review_trace

    matrix_path = config.coverage_matrix_path
    review_path = config.qa_review_path
    if not matrix_path.is_file():
        errors.append(f"缺少文件: {matrix_path}")
    if not review_path.is_file():
        errors.append(f"缺少文件: {review_path}")
    if matrix_path.is_file() and review_path.is_file() and plan_path.is_file():
        try:
            matrix_rows = parse_coverage_matrix(matrix_path)
            review_rows = parse_review_trace(review_path)
            m_err, m_warn = validate_matrix(matrix_rows, requirement_ids, config)
            errors.extend(m_err)
            warnings.extend(m_warn)
            case_ids = {str(c.get("id")) for c in cases if c.get("id")}
            r_err, r_warn = validate_review_trace(
                review_rows,
                {row.scenario_id for row in matrix_rows},
                case_ids,
                config,
            )
            errors.extend(r_err)
            warnings.extend(r_warn)
        except ValueError as exc:
            errors.append(str(exc))

    for warning in warnings:
        print(f"WARNING: {warning}")
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        print(f"FAILED: 共 {len(errors)} 个错误")
        return 1

    print(f"OK: {len(cases)} 条用例全部通过校验，需求条目 {len(requirement_ids)} 条")
    mark_step(config, PipelineStep.VALIDATE)
    return 0


def _run_export(config, cases_path: Path, out_path: Path, plan_path: Path | None, skip_validate: bool) -> int:
    schema = load_schema(config.schema_path)
    try:
        cases = parse_cases(cases_path)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1

    if plan_path and not skip_validate:
        try:
            requirement_ids = parse_requirement_ids(plan_path)
        except (OSError, ValueError) as exc:
            print(f"ERROR: {exc}")
            return 1
        errors, _ = validate_cases(cases, requirement_ids, schema, config)
        if errors:
            for error in errors:
                print(f"ERROR: {error}")
            print("FAILED: 用例未通过校验，拒绝导出。请先修正或使用 --force。")
            return 1

    exporter = get_exporter("xlsx")
    output = exporter.export(ExportContext(output_path=out_path, schema=schema, cases=cases))
    print(f"OK: 已导出 {len(cases)} 条用例到 {output}")
    mark_step(config, PipelineStep.EXPORT)
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    config = resolve_config(overrides={"output_dir": str(args.out) if args.out else None})
    cases_path = args.cases or config.testcases_path
    plan_path = args.plan or config.test_plan_path
    risk_path = args.risk or config.risk_path
    return _run_validate(config, cases_path, plan_path, risk_path)


def cmd_export(args: argparse.Namespace) -> int:
    config = resolve_config(overrides={"output_dir": str(args.out_dir) if args.out_dir else None})
    cases_path = args.cases or config.testcases_path
    plan_path = None if args.force else (args.plan or config.test_plan_path)
    out_path = args.out or config.testcases_xlsx_path
    return _run_export(config, cases_path, out_path, plan_path, skip_validate=args.force)


def cmd_check(args: argparse.Namespace) -> int:
    config = resolve_config(overrides={"output_dir": str(args.out) if args.out else None})
    missing = []
    for label, path in [
        ("test-plan", config.test_plan_path),
        ("risk", config.risk_path),
        ("coverage-matrix", config.coverage_matrix_path),
        ("testcases", config.testcases_path),
        ("qa-review", config.qa_review_path),
    ]:
        if not path.is_file():
            missing.append(f"{label}: {path}")
    if missing:
        for item in missing:
            print(f"ERROR: 缺少产物 {item}")
        return 1
    return _run_validate(
        config,
        config.testcases_path,
        config.test_plan_path,
        config.risk_path,
    )


def cmd_run(args: argparse.Namespace) -> int:
    """独立 Agent 模式：LLM 全自动运行流水线。"""
    from qagent.agent.llm import MockLLM, OpenAILLM
    from qagent.agent.runner import QAgentRunner
    from qagent.ingest import ingest

    config = resolve_config(
        overrides={
            "output_dir": str(args.out) if args.out else None,
        }
    )

    llm_cfg = config.llm
    if args.mock:
        fixtures = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
        llm = MockLLM({
            "test-requirements": (fixtures / "test-requirements-generated.md").read_text(encoding="utf-8"),
            "test-plan": (fixtures / "test-plan.md").read_text(encoding="utf-8"),
            "risk.md": (fixtures / "risk.md").read_text(encoding="utf-8"),
            "coverage-matrix": (fixtures / "coverage-matrix.md").read_text(encoding="utf-8"),
            "qa-review": (fixtures / "qa-review.md").read_text(encoding="utf-8"),
            "testcases": (fixtures / "testcases-valid.md").read_text(encoding="utf-8"),
            "__fix__": (fixtures / "testcases-valid.md").read_text(encoding="utf-8"),
            "__fix_matrix__": (fixtures / "coverage-matrix.md").read_text(encoding="utf-8"),
        })
    else:
        llm = OpenAILLM(llm_cfg, api_key=args.api_key)

    runner = QAgentRunner(config, llm)

    # 文档摄入：--uploads / 目录 / 多文件 / 默认 uploads 目录
    doc_paths: list[Path] = []
    if args.requirement and args.requirement.is_file():
        doc_paths.append(args.requirement.resolve())
    doc_paths.extend(p.resolve() for p in args.docs)

    if args.uploads or (args.requirement and args.requirement.is_dir()) or doc_paths:
        compiled = config.input_dir / "uploads" / "_compiled" / "requirement.md"
        try:
            if doc_paths:
                from qagent.ingest import merge_documents
                merged = merge_documents(doc_paths)
                compiled.parent.mkdir(parents=True, exist_ok=True)
                compiled.write_text(merged, encoding="utf-8")
                print(f"[QAgent] 已合并 {len(doc_paths)} 份文档 → {compiled}")
            else:
                source = config.input_dir / "uploads" if args.uploads else args.requirement
                result = ingest(source, compiled, workspace=config.workspace)
                print(f"[QAgent] 已摄入 {len(result.product_paths)} 份产品文档 → {compiled}")
                if result.test_requirements_text:
                    print("[QAgent] 已加载测试需求（优先遵循）")
            requirement = compiled
        except (OSError, ValueError, ImportError) as exc:
            print(f"ERROR: {exc}")
            return 1
    else:
        uploads = config.input_dir / "uploads"
        compiled = uploads / "_compiled" / "requirement.md"
        if uploads.is_dir() and any(
            p.is_file() and not p.name.startswith(".") for p in uploads.iterdir()
        ):
            try:
                result = ingest(uploads, compiled, workspace=config.workspace)
                print(f"[QAgent] 已摄入 {len(result.product_paths)} 份产品文档 → {compiled}")
                if result.test_requirements_text:
                    print("[QAgent] 已加载测试需求（优先遵循）")
                requirement = compiled
            except (OSError, ValueError, ImportError) as exc:
                print(f"ERROR: {exc}")
                return 1
        else:
            print("ERROR: 请先上传或指定需求文档。方式：")
            print("  qagent serve                 # Web 上传（推荐）")
            print("  qagent run --uploads         # 读取 input/uploads/ 下所有文档")
            print("  qagent run 需求.md 设计.docx  # 指定多个文档")
            return 1

    try:
        result = runner.run(requirement, start_from=args.from_step)
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 1

    if not result.success:
        for error in result.errors:
            print(f"ERROR: {error}")
        print("FAILED: Agent 流水线未完成")
        return 1

    print(f"OK: Agent 完成，{result.case_count} 条用例")
    for name, path in result.artifacts.items():
        print(f"  {name}: {path}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from qagent.server.app import serve
    serve(host=args.host, port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    config = resolve_config(
        overrides={
            "output_dir": str(args.out) if args.out else None,
            "input_dir": str(args.requirement.parent) if args.requirement else None,
        }
    )
    requirement = args.requirement.resolve()
    if not requirement.is_file():
        print(f"ERROR: 需求文件不存在: {requirement}")
        return 1

    init_pipeline(config, requirement)
    print(f"OK: 流水线已初始化，需求: {requirement}")
    print(f"输出目录: {config.output_dir}")
    print("请由 Agent 按 qa-orchestrator 完成生成，再 qagent check")
    print(f"  qagent check --out {config.output_dir}")
    print("或分步:")
    print(f"  qagent validate --out {config.output_dir}")
    print(f"  qagent export --out-dir {config.output_dir}")
    return 0


def cmd_pipeline(args: argparse.Namespace) -> int:
    config = resolve_config(overrides={"output_dir": str(args.out) if args.out else None})

    if args.pipeline_cmd == "status":
        status = pipeline_status(config)
        for step, info in status["steps"].items():
            done = "✓" if info["completed"] or info.get("artifact_exists") else " "
            artifact = info.get("artifact") or "-"
            print(f"[{done}] {step}: {artifact}")
        return 0

    if args.pipeline_cmd == "validate-export":
        pre = check_prerequisites(config, PipelineStep.VALIDATE)
        if pre:
            for err in pre:
                print(f"ERROR: {err}")
            return 1
        rc = _run_validate(
            config,
            config.testcases_path,
            config.test_plan_path,
            config.risk_path,
        )
        if rc != 0:
            return rc
        pre = check_prerequisites(config, PipelineStep.EXPORT)
        if pre:
            for err in pre:
                print(f"ERROR: {err}")
            return 1
        return _run_export(
            config,
            config.testcases_path,
            config.testcases_xlsx_path,
            config.test_plan_path,
            skip_validate=False,
        )

    print(f"ERROR: 未知 pipeline 子命令: {args.pipeline_cmd}")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qagent", description="QAgent 测试方案与用例工具")
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="校验 testcases.md")
    p_validate.add_argument("cases", type=Path, nargs="?", help="testcases.md 路径")
    p_validate.add_argument("--plan", type=Path, help="test-plan.md 路径")
    p_validate.add_argument("--risk", type=Path, help="risk.md 路径")
    p_validate.add_argument("--out", type=Path, help="输出目录（读取 qagent.yaml）")
    p_validate.set_defaults(func=cmd_validate)

    p_export = sub.add_parser("export", help="导出 testcases.xlsx")
    p_export.add_argument("cases", type=Path, nargs="?", help="testcases.md 路径")
    p_export.add_argument("--out", type=Path, help="输出 xlsx 路径")
    p_export.add_argument("--plan", type=Path, help="test-plan.md（校验后导出）")
    p_export.add_argument("--out-dir", type=Path, help="输出目录")
    p_export.add_argument("--force", action="store_true", help="跳过校验强制导出")
    p_export.set_defaults(func=cmd_export)

    p_check = sub.add_parser("check", help="校验 output 目录全部产物")
    p_check.add_argument("--out", type=Path, help="输出目录")
    p_check.set_defaults(func=cmd_check)

    p_run = sub.add_parser("run", help="独立 Agent：上传/指定文档 → 生成测试方案与用例")
    p_run.add_argument("requirement", type=Path, nargs="?", help="单个需求文件或文档目录")
    p_run.add_argument("docs", type=Path, nargs="*", help="多个文档路径")
    p_run.add_argument("--uploads", action="store_true",
                       help="读取 input/uploads/ 下所有已上传文档")
    p_run.add_argument("--out", type=Path, help="输出目录")
    p_run.add_argument("--api-key", type=str, help="LLM API Key（默认读 qagent.local.yaml）")
    p_run.add_argument("--mock", action="store_true", help="使用 Mock LLM（测试/离线）")
    p_run.add_argument(
        "--from",
        dest="from_step",
        default="requirements",
        choices=["requirements", "testcases"],
        help="testcases=复用已有方案/矩阵，只重跑用例及之后步骤",
    )
    p_run.set_defaults(func=cmd_run)

    p_serve = sub.add_parser("serve", help="启动多人任务服务（Web / API / 飞书回调）")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8765)
    p_serve.add_argument("--no-browser", action="store_true")
    p_serve.set_defaults(func=cmd_serve)

    p_generate = sub.add_parser("generate", help="初始化流水线（Step 1-7 由外部 Agent 完成）")
    p_generate.add_argument("requirement", type=Path, help="需求文档路径")
    p_generate.add_argument("--out", type=Path, help="输出目录")
    p_generate.set_defaults(func=cmd_generate)

    p_pipeline = sub.add_parser("pipeline", help="流水线状态与自动化步骤")
    p_pipeline_sub = p_pipeline.add_subparsers(dest="pipeline_cmd", required=True)
    for name, help_text in [("status", "查看步骤状态"), ("validate-export", "执行 Step 8-9")]:
        sub_parser = p_pipeline_sub.add_parser(name, help=help_text)
        sub_parser.add_argument("--out", type=Path, help="输出目录")
        sub_parser.set_defaults(func=cmd_pipeline, pipeline_cmd=name)
    p_pipeline.set_defaults(func=cmd_pipeline)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
