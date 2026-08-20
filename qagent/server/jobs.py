"""每个任务一份目录，避免多人互相覆盖。"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATUSES = ("uploaded", "running", "ready", "revising", "failed", "cancelled")
RESUME_FROM_MATRIX = (
    "test-requirements.md",
    "test-plan.md",
    "risk.md",
    "coverage-matrix.md",
)
ARTIFACT_NAMES = {
    "test-requirements.md": "test_requirements",
    "test-requirements.drawio": "test_requirements_drawio",
    "test-requirements.xmind": "test_requirements_xmind",
    "test-plan.md": "test_plan",
    "risk.md": "risk",
    "coverage-matrix.md": "coverage_matrix",
    "testcases.md": "testcases",
    "qa-review.md": "qa_review",
    "testcases.xlsx": "xlsx",
}
_SAFE_ID = re.compile(r"^[a-zA-Z0-9_-]{8,40}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class JobMeta:
    id: str
    status: str = "uploaded"
    owner: str = "anonymous"
    created_at: str = ""
    updated_at: str = ""
    from_step: str = "requirements"
    logs: list[str] = field(default_factory=list)
    error: list[str] | None = None
    case_count: int | None = None
    artifacts: dict[str, str] = field(default_factory=dict)
    title: str = ""
    feishu_chat_id: str | None = None
    feishu_user_id: str | None = None
    cancel_requested: bool = False
    current_step: str = ""
    awaiting_scope: bool = False

    def to_public(self) -> dict[str, Any]:
        data = asdict(self)
        data["logs"] = self.logs[-80:]
        return data


class JobStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def job_lock(self, job_id: str) -> threading.Lock:
        with self._locks_guard:
            if job_id not in self._locks:
                self._locks[job_id] = threading.Lock()
            return self._locks[job_id]

    def job_dir(self, job_id: str) -> Path:
        if not _SAFE_ID.match(job_id):
            raise ValueError("无效任务 ID")
        path = (self.root / job_id).resolve()
        if self.root.resolve() not in path.parents and path != self.root.resolve():
            raise ValueError("无效任务路径")
        return path

    def input_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "input"

    def output_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "output"

    def meta_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "meta.json"

    def chat_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "chat.jsonl"

    def create(self, owner: str = "anonymous", title: str = "") -> JobMeta:
        job_id = uuid.uuid4().hex[:16]
        directory = self.root / job_id
        (directory / "input").mkdir(parents=True)
        (directory / "output").mkdir(parents=True)
        meta = JobMeta(
            id=job_id,
            owner=owner,
            created_at=utc_now(),
            updated_at=utc_now(),
            title=title or "未命名任务",
        )
        self.save_meta(meta)
        return meta

    def save_meta(self, meta: JobMeta) -> None:
        meta.updated_at = utc_now()
        self.meta_path(meta.id).write_text(
            json.dumps(asdict(meta), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load(self, job_id: str) -> JobMeta:
        path = self.meta_path(job_id)
        if not path.is_file():
            raise FileNotFoundError(f"任务不存在: {job_id}")
        data = json.loads(path.read_text(encoding="utf-8"))
        allowed = {item.name for item in fields(JobMeta)}
        return JobMeta(**{key: value for key, value in data.items() if key in allowed})

    def delete(self, job_id: str) -> None:
        meta = self.load(job_id)
        if meta.status in {"running", "revising"}:
            raise RuntimeError("任务执行中，无法删除")
        directory = self.job_dir(job_id)
        index_path = self.root / "feishu-chats.json"
        if index_path.is_file():
            try:
                mapping = json.loads(index_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                mapping = {}
            changed = False
            for chat_id, bound in list(mapping.items()):
                if bound == job_id:
                    mapping.pop(chat_id, None)
                    changed = True
            if changed:
                index_path.write_text(
                    json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8",
                )
        shutil.rmtree(directory, ignore_errors=False)
        with self._locks_guard:
            self._locks.pop(job_id, None)

    def list_jobs(self, owner: str | None = None) -> list[JobMeta]:
        jobs: list[JobMeta] = []
        for path in sorted(self.root.glob("*/meta.json"), reverse=True):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                allowed = {item.name for item in fields(JobMeta)}
                meta = JobMeta(**{key: value for key, value in data.items() if key in allowed})
            except (OSError, TypeError, json.JSONDecodeError):
                continue
            if owner and meta.owner != owner:
                continue
            jobs.append(meta)
        return jobs

    def append_log(self, job_id: str, message: str) -> None:
        meta = self.load(job_id)
        ts = datetime.now().strftime("%H:%M:%S")
        meta.logs.append(f"[{ts}] {message}")
        meta.logs = meta.logs[-200:]
        self.save_meta(meta)

    def refresh_artifacts(self, job_id: str) -> dict[str, str]:
        out = self.output_dir(job_id)
        found: dict[str, str] = {}
        for filename, key in ARTIFACT_NAMES.items():
            path = out / filename
            if path.is_file():
                found[key] = filename
        meta = self.load(job_id)
        meta.artifacts = found
        self.save_meta(meta)
        return found

    def save_upload(self, job_id: str, filename: str, data: bytes) -> Path:
        safe = Path(filename).name
        if not safe or safe.startswith("."):
            raise ValueError("非法文件名")
        dest = self.input_dir(job_id) / safe
        dest.write_bytes(data)
        meta = self.load(job_id)
        if meta.title in ("", "未命名任务"):
            meta.title = safe
            self.save_meta(meta)
        return dest

    def append_chat(self, job_id: str, role: str, content: str) -> None:
        line = json.dumps(
            {"ts": utc_now(), "role": role, "content": content},
            ensure_ascii=False,
        )
        path = self.chat_path(job_id)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def load_chat(self, job_id: str, limit: int = 40) -> list[dict[str, str]]:
        path = self.chat_path(job_id)
        if not path.is_file():
            return []
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return rows[-limit:]

    def bind_feishu(self, job_id: str, chat_id: str, user_id: str | None) -> None:
        meta = self.load(job_id)
        meta.feishu_chat_id = chat_id
        meta.feishu_user_id = user_id
        self.save_meta(meta)
        index_path = self.root / "feishu-chats.json"
        mapping: dict[str, str] = {}
        if index_path.is_file():
            try:
                mapping = json.loads(index_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                mapping = {}
        mapping[chat_id] = job_id
        index_path.write_text(
            json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8",
        )

    def job_for_feishu_chat(self, chat_id: str) -> str | None:
        index_path = self.root / "feishu-chats.json"
        if not index_path.is_file():
            return None
        try:
            mapping = json.loads(index_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        return mapping.get(chat_id)

    def can_resume_from_matrix(self, job_id: str) -> bool:
        out = self.output_dir(job_id)
        return all((out / name).is_file() for name in RESUME_FROM_MATRIX)


def default_jobs_root() -> Path:
    env = os.environ.get("QAGENT_JOBS_DIR")
    if env:
        return Path(env)
    return Path.cwd() / "data" / "jobs"
