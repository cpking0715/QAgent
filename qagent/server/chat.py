"""修订对话：LLM 只输出 JSON 动作，脚本改文件并校验。

SYSTEM 提示词中的枚举与数值约束从 templates/（schema + rules.yaml）渲染，
不在此处手写，避免契约漂移。
"""

from __future__ import annotations

import json
import re
from typing import Any

from qagent.agent.llm import LLMClient
from qagent.agent.prompts import extract_document
from qagent.config import QAgentConfig
from qagent.parsing import parse_cases, parse_requirement_items
from qagent.rules import load_rules
from qagent.schema import load_schema
from qagent.server.jobs import JobStore
from qagent.server.scope import artifact_has_perf, write_user_scope
from qagent.server.tools import (
    job_config,
    delete_cases,
    patch_plan,
    read_artifact,
    restore_snapshot,
    snapshot_output,
    upsert_cases,
    validate_and_export,
)

_SYSTEM_TEMPLATE = """你是 QAgent 修订助手。只能改当前任务已生成的测试产物，不能编造未上传文档里的 API。
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
- 不要一次输出超过 {max_cases_per_action} 条用例。
- upsert 每条必须带 requirement_ref，且只能是当前方案里已有的 R 编号。
- type 只能是 {type_enum}。性能、压力、SLA 类用例 type 用「功能」，design_method 用「场景法」。
- 用户要补性能/安全等用例时：先对到方案中含 SLA/性能/权限 的 R；没有对应 R 时先 patch_plan 加一条再 upsert。
- 用户只是询问有没有某类用例时，可以只 read_artifact；一旦要求「补充」，必须 upsert。
"""


def system_prompt(config: QAgentConfig) -> str:
    schema = load_schema(config.schema_path)
    rules = load_rules(config.rules_path)
    type_enum = "/".join(sorted(schema.enum_fields.get("type") or [])) or "功能/边界/异常/安全/组合"
    # 模板含 JSON 字面量大括号，只能用 token 替换而非 str.format
    return (
        _SYSTEM_TEMPLATE
        .replace("{max_cases_per_action}", str(rules.chat_max_cases_per_action))
        .replace("{type_enum}", type_enum)
    )


# ---- AI 审阅（类似 agent 的文档评审：带交叉参考材料的一次深读）----

REVIEW_SYSTEM = (
    "你是资深测试架构师，负责审阅测试文档并给出可执行的改进意见。"
    "输出使用简体中文 Markdown；条目要具体、可执行，问题必须引用原文位置；"
    "不要复述文档内容，不要输出与审阅无关的话。"
)

_REVIEW_LABELS = {
    "test-requirements.md": "测试需求",
    "test-plan.md": "测试方案",
    "risk.md": "风险分析",
    "coverage-matrix.md": "覆盖矩阵",
    "testcases.md": "测试用例",
    "qa-review.md": "QA Review",
}

# 审阅某产物时附带的前置产物（交叉检查追溯与口径一致性）
_REVIEW_CONTEXT = {
    "test-plan.md": ("test-requirements.md",),
    "risk.md": ("test-plan.md",),
    "coverage-matrix.md": ("test-plan.md", "risk.md"),
    "testcases.md": ("coverage-matrix.md",),
    "qa-review.md": ("coverage-matrix.md", "testcases.md"),
}

_REVIEW_CONTEXT_BUDGET = 6000  # 每份参考材料的字符预算


def review_label(target: str, name: str) -> str:
    """审阅对象展示名：产物用中文名，输入文件用原文件名。"""
    if target == "input":
        return name
    return _REVIEW_LABELS.get(name, name)


def review_context_names(artifact: str) -> tuple[str, ...]:
    return _REVIEW_CONTEXT.get(artifact, ())


def clip_text(text: str, budget: int = _REVIEW_CONTEXT_BUDGET) -> str:
    if len(text) <= budget:
        return text
    return text[:budget] + "\n…（过长已截断）"


def build_review_prompt(
    label: str, content: str, context: list[tuple[str, str, str]],
) -> str:
    """拼装审阅 prompt：待审文档 + 交叉参考材料 + 输出格式要求。"""
    parts = [f"# 待审阅文档：{label}\n\n{content}"]
    for kind, ctx_label, ctx_text in context:
        parts.append(f"# 参考材料（{kind}）：{ctx_label}\n\n{ctx_text}")
    parts.append(
        f"请审阅《{label}》，输出 Markdown：\n\n"
        "## 总评\n（2-3 句，先给结论）\n\n"
        "## 主要问题\n按严重程度排序逐条列出，每条包含：**问题** / **位置**（引用原文片段）/"
        "**修改建议**（具体到可直接执行）。\n\n"
        "## 遗漏与风险\n结合参考材料交叉检查：遗漏场景、口径不一致、需求-场景-用例追溯断链。\n\n"
        "## 快速改进清单\n3-5 条可直接执行的动作项。"
    )
    return "\n\n---\n\n".join(parts)


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


_EFFECT_OPS = {"patch_plan", "upsert_cases", "delete_cases", "validate_and_export", "rerun"}


def _llm_actions(
    llm: LLMClient,
    system: str,
    base_user: str,
    store: JobStore,
    job_id: str,
    max_rounds: int = 2,
) -> dict:
    """最多两轮：第一轮只读不写时，把读取结果回流给 LLM 再决策一次。"""
    user = base_user
    parsed: dict = {"reply": "", "actions": []}
    for round_index in range(max_rounds):
        parsed = _parse_actions(llm.complete(system, user))
        actions = parsed.get("actions") or []
        reads = [a for a in actions if str(a.get("op") or "") == "read_artifact"]
        has_effect = any(str(a.get("op") or "") in _EFFECT_OPS for a in actions)
        if reads and not has_effect and round_index < max_rounds - 1:
            notes = [
                read_artifact(
                    store, job_id,
                    str(a.get("name") or "plan"),
                    str(a.get("query") or ""),
                )
                for a in reads[:2]
            ]
            user = (
                base_user
                + "\n\n【上一轮 read_artifact 的结果，请据此给出具体修改动作】\n"
                + "\n".join(notes)
            )
            continue
        break
    return parsed


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
        store.update(job_id, lambda m: setattr(m, "awaiting_scope", False))
        reply = "范围已记下，开始生成测试方案和用例。"
        store.append_chat(job_id, "assistant", reply)
        return {"ok": True, "reply": reply, "notes": [], "rerun": "requirements"}

    user = (
        f"{_job_chat_context(store, job_id)}\n"
        f"最近对话：{json.dumps(history, ensure_ascii=False)}\n"
        f"用户：{message}"
    )
    parsed = _llm_actions(llm, system_prompt(job_config(store, job_id)), user, store, job_id)
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
