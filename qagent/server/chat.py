"""修订对话：LLM 只输出 JSON 动作，脚本改文件并校验。"""

from __future__ import annotations

import json
import re
from typing import Any

from qagent.agent.llm import LLMClient
from qagent.agent.prompts import extract_document
from qagent.parsing import parse_cases, parse_requirement_items
from qagent.server.jobs import JobStore
from qagent.server.scope import artifact_has_perf, write_user_scope
from qagent.server.tools import (
    delete_cases,
    patch_plan,
    read_artifact,
    restore_snapshot,
    snapshot_output,
    upsert_cases,
    validate_and_export,
)

SYSTEM = """你是 QAgent 修订助手。只能改当前任务已生成的测试产物，不能编造未上传文档里的 API。
用户要用自然语言补充或修改测试方案/用例。你必须只输出一个 JSON 对象（不要 Markdown 解释），格式：
{
  "reply": "给用户的中文说明",
  "actions": [
    {"op": "read_artifact", "name": "plan|cases|matrix|risk", "query": "可选关键词"},
    {"op": "patch_plan", "add": [{"id": "R99", "text": "描述"}], "edit": {"R1": "新描述"}},
    {"op": "upsert_cases", "cases": [{"id": "TC-XX-001", "title": "...", "priority": "P1", "type": "功能", "preconditions": [], "steps": ["..."], "expected": "...", "design_method": "场景法", "requirement_ref": "R1"}]},
    {"op": "delete_cases", "ids": ["TC-XX-001"]},
    {"op": "validate_and_export", "fill_gaps": true},
    {"op": "rerun", "from_step": "testcases"}
  ]
}
规则：
- 能局部改就不要 rerun。rerun 只在用户明确要求重跑时使用。
- 改完方案或用例后必须带一条 validate_and_export。
- 不要一次输出超过 8 条用例。
- upsert 每条必须带 requirement_ref，且只能是当前方案里已有的 R 编号。
- type 只能是 功能/边界/异常/安全/组合。性能、压力、SLA 类用例 type 用「功能」，design_method 用「场景法」。
- 用户要补性能/安全等用例时：先对到方案中含 SLA/性能/权限 的 R；没有对应 R 时先 patch_plan 加一条再 upsert。
- 用户只是询问有没有某类用例时，可以只 read_artifact；一旦要求「补充」，必须 upsert。
"""


def _parse_actions(text: str) -> dict[str, Any]:
    raw = extract_document(text)
    fenced = re.search(r"```json\s*\n(.*)\n```", raw, re.DOTALL)
    if fenced:
        raw = fenced.group(1)
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return {"reply": raw.strip() or "没有可执行的修改。", "actions": []}
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return {"reply": raw.strip(), "actions": []}
    if not isinstance(data, dict):
        return {"reply": str(data), "actions": []}
    data.setdefault("reply", "")
    data.setdefault("actions", [])
    if not isinstance(data["actions"], list):
        data["actions"] = []
    return data


def apply_actions(
    store: JobStore,
    job_id: str,
    actions: list[dict[str, Any]],
) -> tuple[list[str], dict | None, str | None]:
    """执行动作。返回 (notes, validate_result, rerun_from)。校验失败不抛错，由 run_chat 决定保留或剔除。"""
    notes: list[str] = []
    last_validate: dict | None = None
    rerun_from: str | None = None
    for action in actions:
        op = str(action.get("op") or "")
        if op == "read_artifact":
            notes.append(read_artifact(
                store, job_id,
                str(action.get("name") or "plan"),
                str(action.get("query") or ""),
            ))
        elif op == "patch_plan":
            notes.append(patch_plan(
                store, job_id,
                add=action.get("add") or [],
                edit=action.get("edit") or {},
            ))
        elif op == "upsert_cases":
            notes.append(upsert_cases(store, job_id, action.get("cases") or []))
        elif op == "delete_cases":
            notes.append(delete_cases(store, job_id, action.get("ids") or []))
        elif op == "validate_and_export":
            last_validate = validate_and_export(
                store, job_id,
                fill_gaps=bool(action.get("fill_gaps", True)),
            )
            if not last_validate.get("ok"):
                notes.append(
                    "校验未通过，已保留本次写入："
                    + "; ".join((last_validate.get("errors") or [])[:5])
                )
            else:
                notes.append("校验通过并已更新 Review / xlsx / 思维导图")
        elif op == "rerun":
            step = str(action.get("from_step") or "testcases")
            if step not in {"requirements", "testcases"}:
                step = "testcases"
            rerun_from = step
            notes.append(f"将从 {step} 重跑流水线")
        else:
            notes.append(f"忽略未知动作: {op}")
    return notes, last_validate, rerun_from


def _job_chat_context(store: JobStore, job_id: str) -> str:
    out = store.output_dir(job_id)
    chunks: list[str] = []
    plan = out / "test-plan.md"
    if plan.is_file():
        try:
            items = parse_requirement_items(plan)
        except ValueError:
            items = []
        if items:
            lines = [f"{rid}: {desc[:80]}" for rid, desc in items]
            chunks.append("需求条目（requirement_ref 必须从这里选）：\n" + "\n".join(lines))
    cases_path = out / "testcases.md"
    if cases_path.is_file():
        text = cases_path.read_text(encoding="utf-8")
        n = text.count("```yaml")
        chunks.append(
            f"已有用例约 {n} 条；正文是否含性能/耗时/SLA：{'是' if artifact_has_perf(text) else '否'}"
        )
    return "\n".join(chunks) or "尚无产物"


def _case_ids(store: JobStore, job_id: str) -> set[str]:
    path = store.output_dir(job_id) / "testcases.md"
    if not path.is_file():
        return set()
    try:
        return {str(case.get("id")) for case in parse_cases(path) if case.get("id")}
    except ValueError:
        return set()


def _drop_bad_new_cases(
    store: JobStore,
    job_id: str,
    existing_ids: set[str],
    errors: list[str],
) -> list[str]:
    new_ids = _case_ids(store, job_id) - existing_ids
    bad = [cid for cid in sorted(new_ids) if any(cid in err for err in errors)]
    if bad:
        delete_cases(store, job_id, bad)
    return bad


def run_chat(
    store: JobStore,
    job_id: str,
    message: str,
    llm: LLMClient,
    persist_user: bool = True,
) -> dict[str, Any]:
    history = store.load_chat(job_id, limit=8)
    if persist_user:
        store.append_chat(job_id, "user", message)
    elif history and history[-1].get("role") == "user" and history[-1].get("content") == message:
        history = history[:-1]

    meta = store.load(job_id)
    if meta.awaiting_scope:
        write_user_scope(store, job_id, message)
        meta = store.load(job_id)
        meta.awaiting_scope = False
        store.save_meta(meta)
        reply = "范围已记下，开始生成测试方案和用例。"
        store.append_chat(job_id, "assistant", reply)
        return {"ok": True, "reply": reply, "notes": [], "rerun": "requirements"}

    user = (
        f"{_job_chat_context(store, job_id)}\n"
        f"最近对话：{json.dumps(history, ensure_ascii=False)}\n"
        f"用户：{message}"
    )
    parsed = _parse_actions(llm.complete(SYSTEM, user))
    existing_ids = _case_ids(store, job_id)
    snapshot_output(store, job_id)
    try:
        notes, validated, rerun_from = apply_actions(store, job_id, parsed["actions"])
    except Exception as exc:
        restore_snapshot(store, job_id)
        err = f"修订未生效（已回滚）：{exc}"
        store.append_chat(job_id, "assistant", err)
        return {
            "ok": False,
            "reply": err,
            "notes": [],
            "rerun": None,
        }
    if validated and not validated.get("ok"):
        errors = list(validated.get("errors") or [])
        bad = _drop_bad_new_cases(store, job_id, existing_ids, errors)
        new_left = _case_ids(store, job_id) - existing_ids
        if bad:
            notes.append(f"已剔除未通过校验的用例 {bad}")
        if not new_left and not bad:
            restore_snapshot(store, job_id)
            err = "修订未生效（已回滚）：" + "; ".join(errors[:5])
            store.append_chat(job_id, "assistant", err)
            return {"ok": False, "reply": err, "notes": notes, "rerun": None}
    reply = parsed.get("reply") or "已按你的要求处理。"
    if notes:
        reply = reply + "\n" + "\n".join(f"- {n}" for n in notes if len(n) < 200)
    store.append_chat(job_id, "assistant", reply)
    ok = not (validated and not validated.get("ok"))
    return {
        "ok": ok,
        "reply": reply,
        "notes": notes,
        "rerun": rerun_from,
        "validate": validated,
    }
