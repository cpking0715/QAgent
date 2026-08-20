"""并发与状态存储回归测试：取消竞态、原子写、append-only 日志。

对应 docs/optimization-refactor-plan.md 阶段 1：
- cancel 标志不得被流水线线程的 meta 写入覆盖（W-01）
- meta.json 原子写，不因日志追加而重写（W-02 / P-02）
- 日志 append-only 且并发追加不丢行（P-02）
- 旧版 meta 内嵌日志自动迁移（兼容）
- 损坏的 meta.json 不再让任务静默消失（至少有 warning 日志）
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from qagent.agent.llm import MockLLM
from qagent.server.jobs import JobStore
from qagent.server.service import QAgentService


def test_cancel_flag_survives_concurrent_meta_updates(tmp_path):
    """持续 update 的线程与取消写入并发，取消标志必须保留。"""
    store = JobStore(tmp_path / "jobs")
    job = store.create()
    store.update(job.id, lambda m: setattr(m, "status", "running"))
    stop = threading.Event()

    def churn() -> None:
        n = 0
        while not stop.is_set():
            store.update(job.id, lambda m, n=n: setattr(m, "current_step", f"{n}/9 x"))
            n += 1

    worker = threading.Thread(target=churn)
    worker.start()
    time.sleep(0.05)
    store.update(job.id, lambda m: setattr(m, "cancel_requested", True))
    stop.set()
    worker.join()
    assert store.load(job.id).cancel_requested is True


def test_service_cancel_not_lost_against_step_updates(tmp_path):
    """service.cancel_job 与模拟的步骤上报并发（原 W-01 竞态场景）。"""
    store = JobStore(tmp_path / "jobs")
    service = QAgentService(store, llm_factory=lambda: MockLLM({}), max_pipeline=2)
    job = store.create()
    store.save_upload(job.id, "req.md", b"# x\n")
    store.update(job.id, lambda m: setattr(m, "status", "running"))
    stop = threading.Event()

    def churn() -> None:
        n = 0
        while not stop.is_set():
            store.update(job.id, lambda m, n=n: setattr(m, "current_step", f"{n}/9 run"))
            n += 1

    worker = threading.Thread(target=churn)
    worker.start()
    time.sleep(0.05)
    public = service.cancel_job(job.id)
    assert public["cancel_requested"] is True
    stop.set()
    worker.join()
    assert store.load(job.id).cancel_requested is True


def test_append_log_does_not_rewrite_meta(tmp_path):
    store = JobStore(tmp_path / "jobs")
    job = store.create()
    before = store.meta_path(job.id).read_bytes()
    store.append_log(job.id, "Step 2/9 test-requirements")
    store.append_log(job.id, "Step 3/9 test-plan")
    assert store.meta_path(job.id).read_bytes() == before
    assert not list(store.job_dir(job.id).glob("*.tmp"))


def test_concurrent_append_log_keeps_all_lines(tmp_path):
    store = JobStore(tmp_path / "jobs")
    job = store.create()

    def worker(tag: int) -> None:
        for i in range(50):
            store.append_log(job.id, f"t{tag}-{i:03d}")

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    logs = store.recent_logs(job.id, limit=1000)
    assert len(logs) == 200
    assert all(any(f"t{tag}-{i:03d}" in line for line in logs) for tag in range(4) for i in (0, 49))


def test_append_log_atomic_meta_after_many_writes(tmp_path):
    store = JobStore(tmp_path / "jobs")
    job = store.create()
    for i in range(500):
        store.append_log(job.id, f"line-{i}")
    # meta.json 始终是合法 JSON，且无 tmp 残留
    import json
    data = json.loads(store.meta_path(job.id).read_text(encoding="utf-8"))
    assert data["id"] == job.id
    assert not list(store.job_dir(job.id).glob("*.tmp"))
    # 文件行数被裁剪到有界范围内
    file_lines = len(
        store.logs_path(job.id).read_text(encoding="utf-8").splitlines()
    )
    assert file_lines <= 400


def test_recent_logs_tail_bounded_and_clear(tmp_path):
    store = JobStore(tmp_path / "jobs")
    job = store.create()
    for i in range(250):
        store.append_log(job.id, f"line-{i:03d}")
    logs = store.recent_logs(job.id, limit=1000)
    assert len(logs) == 200
    assert "line-249" in logs[-1]
    assert "line-000" not in logs[0]
    store.clear_logs(job.id)
    assert store.recent_logs(job.id) == []
    assert store.logs_path(job.id).read_text(encoding="utf-8") == ""


def test_legacy_meta_logs_migrated_to_file(tmp_path):
    store = JobStore(tmp_path / "jobs")
    job = store.create()
    meta = store.load(job.id)
    meta.logs = ["[00:00:00] 旧日志"]
    store.save_meta(meta)
    store.append_log(job.id, "新日志")
    logs = store.recent_logs(job.id, limit=10)
    assert logs[0] == "[00:00:00] 旧日志"
    assert logs[-1].endswith("新日志")
    text = store.logs_path(job.id).read_text(encoding="utf-8")
    assert text.count("\n") == 2


def test_list_jobs_skips_corrupt_meta_without_crash(tmp_path):
    root = tmp_path / "jobs"
    store = JobStore(root)
    good = store.create(title="good")
    bad_dir = root / "0bad0bad0bad0bad"
    bad_dir.mkdir()
    (bad_dir / "meta.json").write_text("{broken json", encoding="utf-8")
    jobs = store.list_jobs()
    assert [m.id for m in jobs] == [good.id]


def test_start_run_resets_logs_and_error(tmp_path):
    store = JobStore(tmp_path / "jobs")
    service = QAgentService(store, llm_factory=lambda: MockLLM({}))
    job = store.create()
    store.save_upload(job.id, "req.md", b"# hello\n")
    store.append_log(job.id, "Step 2/9 old")
    store.update(job.id, lambda m: setattr(m, "error", ["旧错误"]))
    public = service.start_run(job.id, "requirements")
    assert public["status"] == "running"
    assert public["error"] is None
    assert public["logs"] == []
