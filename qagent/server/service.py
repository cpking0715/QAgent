"""任务编排：异步跑流水线、对话修订。"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from qagent.agent.llm import LLMCancelled, LLMClient, OpenAILLM
from qagent.agent.runner import JobCancelled, QAgentRunner
from qagent.config import resolve_config
from qagent.deliverables import list_deliverables
from qagent.ingest import ingest
from qagent.server.chat import run_chat
from qagent.server.jobs import ARTIFACT_NAMES, JobStore
from qagent.server.scope import SCOPE_DRAFT, inputs_include_test_requirements
from qagent.server.tools import job_config

logger = logging.getLogger("qagent.server.service")

NFR_FOLLOWUP = (
    "生成已完成。如果还要补 **性能 / 安全 / 兼容** 用例，直接说即可，"
    "例如「补充性能测试用例」。"
)
PHASE_HINT = (
    "「{step}」阶段已生成完毕。可直接在右侧产物抽屉里**编辑修改**，"
    "或对话告诉我调整；确认无误后点「继续下一阶段」。"
)
_CANCEL_WORDS = {"终止", "停止", "取消生成", "取消任务"}


def _public_job(store: JobStore, job_id: str) -> dict:
    data = store.load(job_id).to_public()
    data["logs"] = store.recent_logs(job_id)
    data["chat"] = store.load_chat(job_id)
    data["inputs"] = [
        p.name for p in sorted(store.input_dir(job_id).iterdir()) if p.is_file()
    ]
    data["can_resume_cases"] = store.can_resume_from_matrix(job_id)
    data["deliverables"] = list_deliverables(data.get("artifacts") or {})
    data["stage"] = _next_stage(store, job_id)
    return data


def _next_stage(store: JobStore, job_id: str) -> dict | None:
    """按产物存在性推导分阶段工作流的下一步（人工确认后续跑）。"""
    out = store.output_dir(job_id)

    def has(name: str) -> bool:
        return (out / name).is_file()

    if not has("test-requirements.md"):
        return None
    if not has("coverage-matrix.md"):
        return {
            "done": "test_requirements",
            "label": "生成测试方案（含风险与覆盖矩阵）",
            "from": "test_plan",
            "stop_after": "coverage_matrix",
        }
    if not has("testcases.md"):
        return {
            "done": "coverage_matrix",
            "label": "生成测试用例与 QA Review",
            "from": "testcases",
            "stop_after": None,
        }
    return {
        "done": "export",
        "label": "重跑用例（复用方案与矩阵）",
        "from": "testcases",
        "stop_after": None,
    }


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
        # chat 独立小池：长流水线占满 pipeline 池时，对话修订仍能及时响应
        self._chat_pool = ThreadPoolExecutor(max_workers=4)

    def _llm(self) -> LLMClient:
        return self._llm_factory()

    def create_job(self, owner: str, uploads: list[tuple[str, bytes]], title: str = "") -> dict:
        meta = self.store.create(owner=owner, title=title)
        for name, data in uploads:
            self.store.save_upload(meta.id, name, data)
        awaiting = not inputs_include_test_requirements(self.store, meta.id)
        self.store.update(meta.id, lambda m: setattr(m, "awaiting_scope", awaiting))
        if awaiting:
            self.store.append_chat(meta.id, "assistant", SCOPE_DRAFT)
        return _public_job(self.store, meta.id)

    def get_job(self, job_id: str) -> dict:
        return _public_job(self.store, job_id)

    def list_jobs(self, owner: str | None = None) -> list[dict]:
        out = []
        for meta in self.store.list_jobs(owner):
            item = meta.to_public()
            item["logs"] = self.store.recent_logs(meta.id)
            item["can_resume_cases"] = self.store.can_resume_from_matrix(meta.id)
            out.append(item)
        return out

    def delete_job(self, job_id: str) -> None:
        self.store.delete(job_id)

    def start_run(
        self, job_id: str, from_step: str = "requirements", stop_after: str | None = None,
    ) -> dict:
        meta = self.store.load(job_id)
        if meta.status in {"running", "revising"}:
            raise RuntimeError("任务正在运行")
        if from_step not in {"requirements", "auto", "testcases"}:
            from_step = "requirements"
        if from_step == "testcases" and not self.store.can_resume_from_matrix(job_id):
            raise RuntimeError("尚未生成覆盖矩阵，不能只跑矩阵后")
        self.store.clear_logs(job_id)

        def _mark(m) -> None:
            m.status = "running"
            m.from_step = from_step
            m.error = None
            m.cancel_requested = False
            m.current_step = ""
            m.awaiting_scope = False

        self.store.update(job_id, _mark)
        self._pipeline.submit(self._run_pipeline, job_id, from_step, stop_after)
        public = self.store.load(job_id).to_public()
        public["logs"] = self.store.recent_logs(job_id)
        return public | {
            "can_resume_cases": self.store.can_resume_from_matrix(job_id),
        }

    def cancel_job(self, job_id: str) -> dict:
        meta = self.store.load(job_id)
        if meta.status not in {"running", "revising"}:
            raise RuntimeError("当前没有正在运行的任务")
        # update 内部持 per-job 锁完成 读→改→写，
        # 与流水线线程的 meta 写入串行化，取消标志不会被覆盖丢失
        self.store.update(job_id, lambda m: setattr(m, "cancel_requested", True))
        self.store.append_log(job_id, "收到终止请求，将在当前 LLM 调用结束后停止")
        return _public_job(self.store, job_id)

    def _run_pipeline(self, job_id: str, from_step: str, stop_after: str | None = None) -> None:
        lock = self.store.job_lock(job_id)
        with lock:
            try:
                self._run_pipeline_locked(job_id, from_step, stop_after)
            except (JobCancelled, LLMCancelled):
                def _cancelled(m) -> None:
                    m.status = "cancelled"
                    m.cancel_requested = False
                    m.error = None

                self.store.update(job_id, _cancelled)
                self.store.append_log(job_id, "已终止")
                self.store.refresh_artifacts(job_id)
            except Exception as exc:
                def _failed(m) -> None:
                    m.status = "failed"
                    m.error = [str(exc)]
                    m.cancel_requested = False

                self.store.update(job_id, _failed)
                self.store.append_log(job_id, f"ERROR: {exc}")
                logger.exception("任务 %s 流水线失败", job_id)

    def _run_pipeline_locked(
        self, job_id: str, from_step: str, stop_after: str | None = None,
    ) -> None:
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

        def on_step(step_id: str, index: int, total: int, label: str) -> None:
            value = f"{index}/{total} {label}" if label else f"{index}/{total}"
            self.store.update(
                job_id, lambda m, v=value: setattr(m, "current_step", v),
            )

        def should_cancel() -> bool:
            # load 走内存缓存，不再每条日志读盘
            return bool(self.store.load(job_id).cancel_requested)

        llm = self._llm()
        # 流式模式下 LLM 在 chunk 间检查该回调，取消延迟从"整次调用"降到秒级
        if should_cancel and hasattr(llm, "should_cancel"):
            llm.should_cancel = should_cancel
        runner = QAgentRunner(
            config, llm, on_log=on_log, should_cancel=should_cancel,
            on_step=on_step,
        )
        result = runner.run(
            compiled, start_from=from_step, stop_after=stop_after,
        )
        if self.store.load(job_id).cancel_requested:
            raise JobCancelled("用户终止")
        self.store.update(job_id, lambda m: setattr(m, "case_count", result.case_count))
        self.store.refresh_artifacts(job_id)
        if result.success:
            if result.stopped_after:
                stopped = result.stopped_after

                def _phase_done(m) -> None:
                    m.status = "ready"
                    m.error = None
                    m.case_count = result.case_count
                    m.current_step = f"阶段完成：{stopped}，可修改产物后继续"

                self.store.update(job_id, _phase_done)
                self.store.append_chat(job_id, "assistant", PHASE_HINT.format(step=stopped))
            else:
                def _ready(m) -> None:
                    m.status = "ready"
                    m.error = None
                    m.case_count = result.case_count
                    m.current_step = "9/9 完成"

                self.store.update(job_id, _ready)
                self.store.append_chat(job_id, "assistant", NFR_FOLLOWUP)
        else:
            def _failed(m) -> None:
                m.status = "failed"
                m.error = result.errors

            self.store.update(job_id, _failed)

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
            self.store.update(job_id, lambda m: setattr(m, "status", "revising"))
            self.store.append_chat(job_id, "user", text)
            self.store.append_log(job_id, "正在回复…")
        self._chat_pool.submit(self._run_chat, job_id, text)
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
            self.store.update(job_id, lambda m: setattr(m, "status", "revising"))
            result = self._chat_locked(job_id, text, persist_user=True)
        if result.get("rerun"):
            # 分段工作流：范围确认后先只生成测试需求
            stop = "test_requirements" if result["rerun"] == "requirements" else None
            return {**result, "job": self.start_run(job_id, result["rerun"], stop_after=stop)}
        return {**result, "job": self.get_job(job_id)}

    def _run_chat(self, job_id: str, message: str) -> None:
        lock = self.store.job_lock(job_id)
        with lock:
            result = self._chat_locked(job_id, message, persist_user=False)
        if result.get("rerun"):
            try:
                # 分段工作流：范围确认后先只生成测试需求，人工确认后再继续
                stop = "test_requirements" if result["rerun"] == "requirements" else None
                self.start_run(job_id, result["rerun"], stop_after=stop)
            except RuntimeError as exc:
                logger.warning("任务 %s 对话后重跑失败: %s", job_id, exc)

    def _chat_locked(self, job_id: str, message: str, persist_user: bool) -> dict:
        if self.store.load(job_id).cancel_requested:
            def _cancelled(m) -> None:
                m.status = "cancelled"
                m.cancel_requested = False

            self.store.update(job_id, _cancelled)
            return {"ok": False, "reply": "已终止", "notes": [], "rerun": None}
        try:
            result = run_chat(
                self.store, job_id, message, self._llm(), persist_user=persist_user,
            )
        except Exception as exc:
            self.store.append_chat(job_id, "assistant", f"回复失败：{exc}")
            result = {"ok": False, "reply": str(exc), "notes": [], "rerun": None}
        self.store.refresh_artifacts(job_id)

        def _finalize(m) -> None:
            if m.status == "revising":
                if m.cancel_requested:
                    m.status = "cancelled"
                    m.cancel_requested = False
                else:
                    m.status = "ready" if not m.awaiting_scope else "uploaded"

        self.store.update(job_id, _finalize)
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

    def save_artifact(self, job_id: str, name: str, content: str) -> dict:
        """人工修改产物（分阶段确认工作流）：仅允许编辑 Markdown 类产物。"""
        safe = Path(name).name
        allowed = {
            filename for filename in ARTIFACT_NAMES
            if filename.endswith(".md") and filename != "test-requirements.md"
        }
        allowed.add("test-requirements.md")
        if safe not in allowed:
            raise ValueError(f"仅支持编辑 Markdown 产物: {sorted(allowed)}")
        text = (content or "").strip()
        if not text:
            raise ValueError("内容不能为空")
        path = self.store.output_dir(job_id) / safe
        if not path.is_file():
            raise FileNotFoundError(safe)
        path.write_text(content, encoding="utf-8")
        if safe == "test-requirements.md":
            # 需求导图随正文更新
            try:
                from qagent.exporters.mindmap import (
                    write_requirements_drawio,
                    write_requirements_xmind,
                )

                write_requirements_drawio(path, self.store.output_dir(job_id) / "test-requirements.drawio")
                write_requirements_xmind(path, self.store.output_dir(job_id) / "test-requirements.xmind")
            except (OSError, ValueError) as exc:
                logger.warning("需求导图更新失败 job=%s: %s", job_id, exc)
        self.store.refresh_artifacts(job_id)
        return self.get_job(job_id)
