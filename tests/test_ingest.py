"""文档摄入测试。"""

from pathlib import Path

from qagent.ingest import (
    IngestResult,
    collect_documents,
    ingest,
    is_test_requirements_file,
    merge_documents,
)

FIXTURES = Path(__file__).parent / "fixtures"
REPO = Path(__file__).resolve().parents[1]


def test_merge_two_markdown_files(tmp_path):
    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    a.write_text("# A\n规则1", encoding="utf-8")
    b.write_text("# B\n规则2", encoding="utf-8")
    merged = merge_documents([a, b])
    assert "文档: a.md" in merged
    assert "文档: b.md" in merged
    assert "规则1" in merged and "规则2" in merged


def test_test_requirements_section(tmp_path):
    prd = tmp_path / "prd.md"
    treq = tmp_path / "测试需求.md"
    prd.write_text("# PRD\nR1", encoding="utf-8")
    treq.write_text("# 测试范围\n必测 API", encoding="utf-8")
    merged = merge_documents([prd, treq])
    assert "## 测试需求" in merged
    assert "必测 API" in merged
    assert "产品需求文档" in merged


def test_is_test_requirements_file():
    assert is_test_requirements_file(Path("test-requirements.md"))
    assert is_test_requirements_file(Path("测试需求.md"))
    assert not is_test_requirements_file(Path("OCR-PRD.pdf"))


def test_ingest_directory(tmp_path):
    src = tmp_path / "uploads"
    src.mkdir()
    (src / "req.md").write_text("# 需求\nR1", encoding="utf-8")
    compiled = tmp_path / "compiled.md"
    result = ingest(src, compiled, workspace=tmp_path)
    assert isinstance(result, IngestResult)
    assert compiled.is_file()
    assert len(result.product_paths) == 1
    assert "R1" in result.requirement_text


def test_collect_from_repo_input():
    paths = collect_documents(REPO / "input" / "requirement-example.md")
    assert len(paths) == 1
