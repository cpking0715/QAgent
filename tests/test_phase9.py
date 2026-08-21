"""打开方式枚举与选择：按扩展名匹配本机应用，指定应用打开。"""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from qagent.agent.llm import MockLLM
from qagent.server.jobs import JobStore
from qagent.server.openwith import list_apps_for_extension
from qagent.server.service import QAgentService

FIXTURES = Path(__file__).parent / "fixtures"


def _fake_app(root: Path, name: str, exts: list[str]) -> None:
    contents = root / f"{name}.app" / "Contents"
    contents.mkdir(parents=True, exist_ok=True)
    (contents / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleName": name,
        "CFBundleDocumentTypes": [{"CFBundleTypeExtensions": exts}],
    }))


def test_list_apps_matches_extension(tmp_path):
    _fake_app(tmp_path, "EditorA", ["md", "markdown"])
    _fake_app(tmp_path, "EditorB", ["*"])
    _fake_app(tmp_path, "SheetApp", ["xlsx"])
    out = list_apps_for_extension("md", app_dirs=[tmp_path])
    names = [a["name"] for a in out]
    assert "EditorA" in names  # 精确匹配
    assert "EditorB" in names  # 声明 * 的应用排后但包含
    assert "SheetApp" not in names
    assert names.index("EditorA") < names.index("EditorB")  # 精确在前
    assert all("path" in a and a["path"].endswith(".app") for a in out)


def test_list_apps_empty_for_blank(tmp_path):
    assert list_apps_for_extension("", app_dirs=[tmp_path]) == []
    assert list_apps_for_extension(".", app_dirs=[tmp_path]) == []


def _job_with_plan(tmp_path):
    store = JobStore(tmp_path / "jobs")
    service = QAgentService(store, llm_factory=lambda: MockLLM({}))
    job = store.create()
    store.save_upload(job.id, "prd.md", b"# x\n")
    (store.output_dir(job.id) / "test-plan.md").write_text(
        (FIXTURES / "test-plan.md").read_text(encoding="utf-8"), encoding="utf-8",
    )
    store.refresh_artifacts(job.id)
    return service, job.id


def test_list_open_with_returns_apps(tmp_path, monkeypatch):
    service, job_id = _job_with_plan(tmp_path)
    monkeypatch.setattr(
        "qagent.server.service.list_apps_for_extension",
        lambda ext: [{"name": "EditorA", "path": "/Applications/EditorA.app"}],
    )
    data = service.list_open_with(job_id, "artifact", "test-plan.md")
    assert data["ext"] == "md"
    assert data["apps"][0]["name"] == "EditorA"


def test_open_file_with_selected_app(tmp_path, monkeypatch):
    service, job_id = _job_with_plan(tmp_path)
    calls: list[list[str]] = []

    class FakeProc:
        returncode = 0
        stderr = b""

    monkeypatch.setattr(
        "qagent.server.service.list_apps_for_extension",
        lambda ext: [{"name": "EditorA", "path": "/Applications/EditorA.app"}],
    )
    monkeypatch.setattr(
        "qagent.server.service.subprocess.run",
        lambda cmd, **kw: calls.append(cmd) or FakeProc(),
    )
    assert service.open_file(job_id, "artifact", "test-plan.md", app="EditorA")["ok"]
    assert calls[-1][:2] == ["open", "-a"]
    assert calls[-1][2] == "EditorA"
    assert calls[-1][-1].endswith("test-plan.md")


def test_open_file_rejects_unknown_app(tmp_path, monkeypatch):
    service, job_id = _job_with_plan(tmp_path)
    monkeypatch.setattr(
        "qagent.server.service.list_apps_for_extension",
        lambda ext: [{"name": "EditorA", "path": "/Applications/EditorA.app"}],
    )
    with pytest.raises(ValueError, match="未找到可打开"):
        service.open_file(job_id, "artifact", "test-plan.md", app="恶意应用;rm")
