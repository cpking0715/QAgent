"""每个任务一份目录，避免多人互相覆盖。

状态存储约定：
- meta.json 只通过 save_meta/update 写入，写法为 tmp + os.replace 原子替换；
- 所有 读→改→写 必须走 update()（内部持 per-job RLock），防止并发覆盖丢字段；
- 运行日志写 logs.txt（append-only），meta 不再随日志重写；
- meta 内存缓存（_meta_cache）作为运行期读来源，list_jobs 不再逐个解析 JSON。
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import shutil
import threading
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("qagent.server.jobs")

STATUSES = ("uploaded", "running", "ready", "revising", "failed", "cancelled")
RESUME_FROM_MATRIX = (
    "test-requirements.md",
    "test-plan.md",
    "risk.md",
    "coverage-matrix.md",
)
ARTIFACT_NAMES = {
    "test-requirements.md": "test_requirements",
    "test-requirements.xmind": "test_requirements_xmind",
    "test-plan.md": "test_plan",
    "risk.md": "risk",
    "coverage-matrix.md": "coverage_matrix",
    "testcases.md": "testcases",
    "qa-review.md": "qa_review",
    "testcases.xlsx": "xlsx",
}
_SAFE_ID = re.compile(r"^[a-zA-Z0-9_-]{8,40}$")

LOG_TAIL_LINES = 200    # 内存与 API 展示保留的日志条数
LOG_FILE_TRIM_AT = 400  # logs.txt 超过该行数时裁剪回 LOG_TAIL_LINES


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
    # logs 仅用于兼容旧数据（无 logs.txt 的任务首次读取时迁移），
    # 新日志一律写 logs.txt，不再随每条日志重写 meta。
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
        self._meta_locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._meta_cache: dict[str, JobMeta] = {}
        # job_id -> (最近日志 deque, logs.txt 行数)
        self._log_state: dict[str, tuple[deque, int]] = {}

    def job_lock(self, job_id: str) -> threading.Lock:
        with self._locks_guard:
            if job_id not in self._locks:
                self._locks[job_id] = threading.Lock()
            return self._locks[job_id]

    def _meta_lock(self, job_id: str) -> threading.RLock:
        with self._locks_guard:
            if job_id not in self._meta_locks:
                self._meta_locks[job_id] = threading.RLock()
            return self._meta_locks[job_id]

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

    def logs_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "logs.txt"

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
        with self._meta_lock(meta.id):
            self._save_locked(meta)

    def update(self, job_id: str, mutator: Callable[[JobMeta], None]) -> JobMeta:
        """持锁完成 读→改→写，返回更新后的副本。"""
        with self._meta_lock(job_id):
            meta = self._load_locked(job_id)
            mutator(meta)
            meta.updated_at = utc_now()
            self._save_locked(meta)
            return copy.deepcopy(meta)

    def _save_locked(self, meta: JobMeta) -> None:
        path = self.meta_path(meta.id)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(asdict(meta), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, path)
        self._meta_cache[meta.id] = copy.deepcopy(meta)

    def load(self, job_id: str) -> JobMeta:
        with self._meta_lock(job_id):
            return self._load_locked(job_id)

    def _load_locked(self, job_id: str) -> JobMeta:
        cached = self._meta_cache.get(job_id)
        if cached is not None:
            return copy.deepcopy(cached)
        path = self.meta_path(job_id)
        if not path.is_file():
            raise FileNotFoundError(f"任务不存在: {job_id}")
        data = json.loads(path.read_text(encoding="utf-8"))
        allowed = {item.name for item in fields(JobMeta)}
        meta = JobMeta(**{key: value for key, value in data.items() if key in allowed})
        self._meta_cache[job_id] = copy.deepcopy(meta)
        return meta

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
            self._meta_locks.pop(job_id, None)
            self._meta_cache.pop(job_id, None)
            self._log_state.pop(job_id, None)

    def list_jobs(self, owner: str | None = None) -> list[JobMeta]:
        # 只做目录级扫描发现新任务，meta 读取走内存缓存
        for path in self.root.glob("*/meta.json"):
            job_id = path.parent.name
            if job_id in self._meta_cache:
                continue
            try:
                self.load(job_id)
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                logger.warning("meta.json 读取失败，任务已跳过: %s", path)
        jobs = [
            meta for job_id, meta in self._meta_cache.items()
            if (self.root / job_id).is_dir()
        ]
        jobs.sort(key=lambda m: (m.created_at, m.id), reverse=True)
        if owner:
            jobs = [m for m in jobs if m.owner == owner]
        return [copy.deepcopy(m) for m in jobs]

    def _log_tail(self, job_id: str) -> tuple[deque, int]:
        """惰性加载日志状态；需已持 _meta_lock。"""
        state = self._log_state.get(job_id)
        if state is None:
            path = self.logs_path(job_id)
            if path.exists():
                lines = [
                    line for line in
                    path.read_text(encoding="utf-8", errors="replace").splitlines()
                    if line.strip()
                ]
            else:
                # 兼容旧版：日志内嵌在 meta.logs 里
                lines = list(self._load_locked(job_id).logs)
            state = (
                deque(lines[-LOG_TAIL_LINES:], maxlen=LOG_TAIL_LINES),
                len(lines),
            )
            self._log_state[job_id] = state
        return state

    def append_log(self, job_id: str, message: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
        with self._meta_lock(job_id):
            lines, count = self._log_tail(job_id)
            path = self.logs_path(job_id)
            if not path.exists():
                # 首次写文件时迁移旧 meta 内嵌日志
                path.write_text("".join(f"{item}\n" for item in lines), encoding="utf-8")
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            lines.append(line)
            count += 1
            if count > LOG_FILE_TRIM_AT:
                keep = list(lines)
                path.write_text("".join(f"{item}\n" for item in keep), encoding="utf-8")
                count = len(keep)
            self._log_state[job_id] = (lines, count)

    def recent_logs(self, job_id: str, limit: int = 80) -> list[str]:
        with self._meta_lock(job_id):
            lines, _ = self._log_tail(job_id)
        return list(lines)[-limit:]

    def clear_logs(self, job_id: str) -> None:
        with self._meta_lock(job_id):
            self.logs_path(job_id).write_text("", encoding="utf-8")
            self._log_state[job_id] = (deque(maxlen=LOG_TAIL_LINES), 0)

    def refresh_artifacts(self, job_id: str) -> dict[str, str]:
        out = self.output_dir(job_id)
        found: dict[str, str] = {}
        for filename, key in ARTIFACT_NAMES.items():
            path = out / filename
            if path.is_file():
                found[key] = filename
        self.update(job_id, lambda m: setattr(m, "artifacts", found))
        return found

    def save_upload(self, job_id: str, filename: str, data: bytes) -> Path:
        safe = Path(filename).name
        if not safe or safe.startswith("."):
            raise ValueError("非法文件名")
        dest = self.input_dir(job_id) / safe
        dest.write_bytes(data)

        def _title(meta: JobMeta) -> None:
            if meta.title in ("", "未命名任务"):
                meta.title = safe

        self.update(job_id, _title)
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
        def _bind(meta: JobMeta) -> None:
            meta.feishu_chat_id = chat_id
            meta.feishu_user_id = user_id

        self.update(job_id, _bind)
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

    def mark_stale_on_startup(self) -> int:
        """服务重启后把遗留的 running/revising 任务标记为中断（可从产物续跑）。

        不自动续跑，避免重启循环里反复消耗 LLM 调用。
        """
        count = 0
        for meta in self.list_jobs():
            if meta.status not in {"running", "revising"}:
                continue

            def _stale(m: JobMeta) -> None:
                m.status = "failed"
                m.error = ["服务重启导致任务中断，可用「从矩阵续跑」或 from=auto 继续"]
                m.cancel_requested = False

            self.update(meta.id, _stale)
            self.append_log(meta.id, "服务重启，任务中断")
            count += 1
        return count


def default_jobs_root() -> Path:
    env = os.environ.get("QAGENT_JOBS_DIR")
    if env:
        return Path(env)
    return Path.cwd() / "data" / "jobs"
