"""任务编排：异步跑流水线、对话修订。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from qagent.agent.llm import LLMClient, OpenAILLM
from qagent.agent.runner import QAgentRunner
from qagent.config import resolve_config
from qagent.ingest import ingest
from qagent.server.chat import run_chat
from qagent.server.jobs import JobStore
from qagent.server.tools import job_config


class QAgentService:
    def __init__(
        self,
        store: JobStore,
        llm_factory: Callable[[], LLMClient] | None = None,
        max_pipeline: int = 1,
    ) -> None:
        self.store = store
        self._llm_factory = llm_factory or (lambda: OpenAILLM(resolve_config().llm))
        self._pipeline = ThreadPoolExecutor(max_workers=max_pipeline)

    def _llm(self) -> LLMClient:
        return self._llm_factory()

    def create_job(self, owner: str, uploads: list[tuple[str, bytes]], title: str = "") -> dict:
        meta = self.store.create(owner=owner, title=title)
        for name, data in uploads:
            self.store.save_upload(meta.id, name, data)
        return self.store.load(meta.id).to_public()

    def get_job(self, job_id: str) -> dict:
        meta = self.store.load(job_id)
        data = meta.to_public()
        data["chat"] = self.store.load_chat(job_id)
        data["inputs"] = [p.name for p in sorted(self.store.input_dir(job_id).iterdir()) if p.is_file()]
        return data

    def list_jobs(self, owner: str | None = None) -> list[dict]:
        return [m.to_public() for m in self.store.list_jobs(owner)]

    def start_run(self, job_id: str, from_step: str = "requirements") -> dict:
        meta = self.store.load(job_id)
        if meta.status == "running":
            raise RuntimeError("任务正在运行")
        if from_step not in {"requirements", "testcases"}:
            from_step = "requirements"
        meta.status = "running"
        meta.from_step = from_step
        meta.error = None
        self.store.save_meta(meta)
        self._pipeline.submit(self._run_pipeline, job_id, from_step)
        return self.store.load(job_id).to_public()

    def _run_pipeline(self, job_id: str, from_step: str) -> None:
        lock = self.store.job_lock(job_id)
        with lock:
            try:
                self._run_pipeline_locked(job_id, from_step)
            except Exception as exc:
                meta = self.store.load(job_id)
                meta.status = "failed"
                meta.error = [str(exc)]
                self.store.save_meta(meta)
                self.store.append_log(job_id, f"ERROR: {exc}")

    def _run_pipeline_locked(self, job_id: str, from_step: str) -> None:
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

        runner = QAgentRunner(config, self._llm(), on_log=on_log)
        result = runner.run(compiled, start_from=from_step)
        meta = self.store.load(job_id)
        meta.case_count = result.case_count
        self.store.refresh_artifacts(job_id)
        if result.success:
            meta = self.store.load(job_id)
            meta.status = "ready"
            meta.error = None
            meta.case_count = result.case_count
            self.store.save_meta(meta)
        else:
            meta = self.store.load(job_id)
            meta.status = "failed"
            meta.error = result.errors
            self.store.save_meta(meta)

    def chat(self, job_id: str, message: str) -> dict:
        meta = self.store.load(job_id)
        if meta.status == "running":
            raise RuntimeError("流水线运行中，稍后再对话")
        lock = self.store.job_lock(job_id)
        with lock:
            meta = self.store.load(job_id)
            meta.status = "revising"
            self.store.save_meta(meta)
            try:
                result = run_chat(self.store, job_id, message, self._llm())
            finally:
                meta = self.store.load(job_id)
                if meta.status == "revising":
                    meta.status = "ready" if self.store.refresh_artifacts(job_id) else meta.status
                    self.store.save_meta(meta)
        if result.get("rerun"):
            return {**result, "job": self.start_run(job_id, result["rerun"])}
        return {**result, "job": self.store.load(job_id).to_public()}

    def artifact_path(self, job_id: str, name: str) -> Path:
        safe = Path(name).name
        path = (self.store.output_dir(job_id) / safe).resolve()
        out = self.store.output_dir(job_id).resolve()
        if out not in path.parents and path != out:
            raise ValueError("非法产物路径")
        if not path.is_file():
            raise FileNotFoundError(safe)
        return path
