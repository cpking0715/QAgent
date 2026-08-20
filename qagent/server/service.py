"""任务编排：异步跑流水线、对话修订。"""

from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from qagent.agent.llm import LLMClient, OpenAILLM
from qagent.agent.runner import JobCancelled, QAgentRunner
from qagent.config import resolve_config
from qagent.deliverables import list_deliverables
from qagent.ingest import ingest
from qagent.server.chat import run_chat
from qagent.server.jobs import JobStore
from qagent.server.scope import SCOPE_DRAFT, inputs_include_test_requirements
from qagent.server.tools import job_config

NFR_FOLLOWUP = (
    "生成已完成。如果还要补 **性能 / 安全 / 兼容** 用例，直接说即可，"
    "例如「补充性能测试用例」。"
)
_STEP_RE = re.compile(r"Step\s+(\d+)/(\d+)\s+(\S+)")
_CANCEL_WORDS = {"终止", "停止", "取消生成", "取消任务"}


def _public_job(store: JobStore, job_id: str) -> dict:
    data = store.load(job_id).to_public()
    data["chat"] = store.load_chat(job_id)
    data["inputs"] = [
        p.name for p in sorted(store.input_dir(job_id).iterdir()) if p.is_file()
    ]
    data["can_resume_cases"] = store.can_resume_from_matrix(job_id)
    data["deliverables"] = list_deliverables(data.get("artifacts") or {})
    return data


class QAgentService:
    def __init__(
        self,
        store: JobStore,
        llm_factory: Callable[[], LLMClient] | None = None,
        max_pipeline: int | None = None,
    ) -> None:
        self.store = store
        self._llm_factory = llm_factory or (lambda: OpenAILLM(resolve_config().llm))
        workers = max_pipeline
        if workers is None:
            try:
                workers = int(os.environ.get("QAGENT_MAX_PIPELINE", "8"))
            except ValueError:
                workers = 8
        self._pipeline = ThreadPoolExecutor(max_workers=max(1, workers))

    def _llm(self) -> LLMClient:
        return self._llm_factory()

    def create_job(self, owner: str, uploads: list[tuple[str, bytes]], title: str = "") -> dict:
        meta = self.store.create(owner=owner, title=title)
        for name, data in uploads:
            self.store.save_upload(meta.id, name, data)
        meta = self.store.load(meta.id)
        if inputs_include_test_requirements(self.store, meta.id):
            meta.awaiting_scope = False
            self.store.save_meta(meta)
        else:
            meta.awaiting_scope = True
            self.store.save_meta(meta)
            self.store.append_chat(meta.id, "assistant", SCOPE_DRAFT)
        return _public_job(self.store, meta.id)

    def get_job(self, job_id: str) -> dict:
        return _public_job(self.store, job_id)

    def list_jobs(self, owner: str | None = None) -> list[dict]:
        return [
            {**m.to_public(), "can_resume_cases": self.store.can_resume_from_matrix(m.id)}
            for m in self.store.list_jobs(owner)
        ]

    def delete_job(self, job_id: str) -> None:
        self.store.delete(job_id)

    def start_run(self, job_id: str, from_step: str = "requirements") -> dict:
        meta = self.store.load(job_id)
        if meta.status in {"running", "revising"}:
            raise RuntimeError("任务正在运行")
        if from_step not in {"requirements", "testcases"}:
            from_step = "requirements"
        if from_step == "testcases" and not self.store.can_resume_from_matrix(job_id):
            raise RuntimeError("尚未生成覆盖矩阵，不能只跑矩阵后")
        meta.status = "running"
        meta.from_step = from_step
        meta.error = None
        meta.logs = []
        meta.cancel_requested = False
        meta.current_step = ""
        meta.awaiting_scope = False
        self.store.save_meta(meta)
        self._pipeline.submit(self._run_pipeline, job_id, from_step)
        return self.store.load(job_id).to_public() | {
            "can_resume_cases": self.store.can_resume_from_matrix(job_id),
        }

    def cancel_job(self, job_id: str) -> dict:
        meta = self.store.load(job_id)
        if meta.status not in {"running", "revising"}:
            raise RuntimeError("当前没有正在运行的任务")
        meta.cancel_requested = True
        self.store.save_meta(meta)
        self.store.append_log(job_id, "收到终止请求，将在当前 LLM 调用结束后停止")
        return _public_job(self.store, job_id)

    def _run_pipeline(self, job_id: str, from_step: str) -> None:
        lock = self.store.job_lock(job_id)
        with lock:
            try:
                self._run_pipeline_locked(job_id, from_step)
            except JobCancelled:
                meta = self.store.load(job_id)
                meta.status = "cancelled"
                meta.cancel_requested = False
                meta.error = None
                self.store.save_meta(meta)
                self.store.append_log(job_id, "已终止")
                self.store.refresh_artifacts(job_id)
            except Exception as exc:
                meta = self.store.load(job_id)
                meta.status = "failed"
                meta.error = [str(exc)]
                meta.cancel_requested = False
                self.store.save_meta(meta)
                self.store.append_log(job_id, f"ERROR: {exc}")

    def _run_pipeline_locked(self, job_id: str, from_step: str) -> None:
        if self.store.load(job_id).cancel_requested:
            raise JobCancelled("用户终止")
        config = job_config(self.store, job_id)
        compiled = self.store.input_dir(job_id) / "_compiled" / "requirement.md"
        inputs = [
            p for p in self.store.input_dir(job_id).iterdir()
            if p.is_file() and not p.name.startswith(".")
        ]
        if inputs:
            ingest(self.store.input_dir(job_id), compiled, workspace=self.store.job_dir(job_id))
        elif compiled.is_file():
            pass
        else:
            compiled.parent.mkdir(parents=True, exist_ok=True)
            compiled.write_text("# 空需求\n", encoding="utf-8")

        def on_log(message: str) -> None:
            self.store.append_log(job_id, message)
            matched = _STEP_RE.search(message)
            if matched:
                meta = self.store.load(job_id)
                meta.current_step = f"{matched.group(1)}/{matched.group(2)} {matched.group(3)}"
                self.store.save_meta(meta)

        def should_cancel() -> bool:
            return bool(self.store.load(job_id).cancel_requested)

        runner = QAgentRunner(
            config, self._llm(), on_log=on_log, should_cancel=should_cancel,
        )
        result = runner.run(compiled, start_from=from_step)
        if self.store.load(job_id).cancel_requested:
            raise JobCancelled("用户终止")
        meta = self.store.load(job_id)
        meta.case_count = result.case_count
        self.store.refresh_artifacts(job_id)
        if result.success:
            meta = self.store.load(job_id)
            meta.status = "ready"
            meta.error = None
            meta.case_count = result.case_count
            meta.current_step = "9/9 完成"
            self.store.save_meta(meta)
            self.store.append_chat(job_id, "assistant", NFR_FOLLOWUP)
        else:
            meta = self.store.load(job_id)
            meta.status = "failed"
            meta.error = result.errors
            self.store.save_meta(meta)

    def start_chat(self, job_id: str, message: str) -> dict:
        """立刻落盘用户消息并异步回复，避免 Web 端空等 LLM。"""
        text = (message or "").strip()
        if not text:
            raise ValueError("消息不能为空")
        if text in _CANCEL_WORDS:
            return self.cancel_job(job_id)
        lock = self.store.job_lock(job_id)
        with lock:
            meta = self.store.load(job_id)
            if meta.status == "running":
                raise RuntimeError("流水线运行中，稍后再对话")
            if meta.status == "revising":
                raise RuntimeError("正在回复上一条消息")
            meta.status = "revising"
            self.store.save_meta(meta)
            self.store.append_chat(job_id, "user", text)
            self.store.append_log(job_id, "正在回复…")
        self._pipeline.submit(self._run_chat, job_id, text)
        return self.get_job(job_id)

    def chat(self, job_id: str, message: str) -> dict:
        """同步修订（飞书等需要拿到 reply 再回消息）。"""
        text = (message or "").strip()
        if not text:
            raise ValueError("消息不能为空")
        if text in _CANCEL_WORDS:
            public = self.cancel_job(job_id)
            return {"ok": True, "reply": "已请求终止当前任务。", "notes": [], "rerun": None, "job": public}
        lock = self.store.job_lock(job_id)
        with lock:
            meta = self.store.load(job_id)
            if meta.status == "running":
                raise RuntimeError("流水线运行中，稍后再对话")
            if meta.status == "revising":
                raise RuntimeError("正在回复上一条消息")
            meta.status = "revising"
            self.store.save_meta(meta)
            result = self._chat_locked(job_id, text, persist_user=True)
        if result.get("rerun"):
            return {**result, "job": self.start_run(job_id, result["rerun"])}
        return {**result, "job": self.get_job(job_id)}

    def _run_chat(self, job_id: str, message: str) -> None:
        lock = self.store.job_lock(job_id)
        with lock:
            result = self._chat_locked(job_id, message, persist_user=False)
        if result.get("rerun"):
            try:
                self.start_run(job_id, result["rerun"])
            except RuntimeError:
                pass

    def _chat_locked(self, job_id: str, message: str, persist_user: bool) -> dict:
        if self.store.load(job_id).cancel_requested:
            meta = self.store.load(job_id)
            meta.status = "cancelled"
            meta.cancel_requested = False
            self.store.save_meta(meta)
            return {"ok": False, "reply": "已终止", "notes": [], "rerun": None}
        try:
            result = run_chat(
                self.store, job_id, message, self._llm(), persist_user=persist_user,
            )
        except Exception as exc:
            self.store.append_chat(job_id, "assistant", f"回复失败：{exc}")
            result = {"ok": False, "reply": str(exc), "notes": [], "rerun": None}
        self.store.refresh_artifacts(job_id)
        meta = self.store.load(job_id)
        if meta.status == "revising":
            if meta.cancel_requested:
                meta.status = "cancelled"
                meta.cancel_requested = False
            else:
                meta.status = "ready" if not meta.awaiting_scope else "uploaded"
            self.store.save_meta(meta)
        return result

    def artifact_path(self, job_id: str, name: str) -> Path:
        safe = Path(name).name
        path = (self.store.output_dir(job_id) / safe).resolve()
        out = self.store.output_dir(job_id).resolve()
        if out not in path.parents and path != out:
            raise ValueError("非法产物路径")
        if not path.is_file():
            raise FileNotFoundError(safe)
        return path
