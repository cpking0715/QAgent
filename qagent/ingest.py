"""多文档摄入：读取、合并；区分产品需求与测试需求。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

SUPPORTED_TEXT = {".md", ".txt", ".markdown"}
SUPPORTED_BINARY = {".pdf", ".docx", ".doc"}
SUPPORTED = SUPPORTED_TEXT | SUPPORTED_BINARY

TEST_REQUIREMENTS_NAMES = {
    "test-requirements.md",
    "test-requirements.txt",
    "测试需求.md",
    "测试需求.txt",
}


@dataclass
class IngestResult:
    requirement_text: str
    test_requirements_text: str | None
    product_paths: list[Path]
    test_requirements_paths: list[Path]
    compiled_path: Path | None = None


def _read_text_file(path: Path) -> str:
    for encoding in ("utf-8", "utf-8-sig", "gbk"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def _read_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ImportError(
            f"读取 PDF 需要安装 pypdf: pip install pypdf ({path.name})"
        ) from exc
    reader = PdfReader(str(path))
    parts = []
    for i, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        if text.strip():
            parts.append(f"### 第 {i} 页\n{text.strip()}")
    return "\n\n".join(parts)


def _read_docx(path: Path) -> str:
    try:
        from docx import Document
    except ImportError as exc:
        raise ImportError(
            f"读取 Word 需要安装 python-docx: pip install python-docx ({path.name})"
        ) from exc
    doc = Document(str(path))
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())


def read_document(path: Path) -> str:
    """读取单个文档为纯文本。"""
    path = path.resolve()
    suffix = path.suffix.lower()
    if suffix in SUPPORTED_TEXT:
        return _read_text_file(path)
    if suffix == ".pdf":
        return _read_pdf(path)
    if suffix in {".docx", ".doc"}:
        return _read_docx(path)
    raise ValueError(f"不支持的文件类型: {suffix}（支持: {sorted(SUPPORTED)}）")


def is_test_requirements_file(path: Path) -> bool:
    name = path.name.lower()
    if name in {n.lower() for n in TEST_REQUIREMENTS_NAMES}:
        return True
    return name.startswith("测试需求.") or name.startswith("test-requirements.")


def collect_documents(source: Path, *, include_test_req: bool = True) -> list[Path]:
    """从文件或目录收集文档列表。"""
    source = source.resolve()
    if source.is_file():
        return [source]
    if source.is_dir():
        files = [
            p for p in sorted(source.iterdir())
            if p.is_file() and not p.name.startswith(".")
            and p.suffix.lower() in SUPPORTED
        ]
        if not files:
            raise FileNotFoundError(f"目录中没有可识别的文档: {source}")
        return files
    raise FileNotFoundError(f"路径不存在: {source}")


def split_document_paths(paths: list[Path]) -> tuple[list[Path], list[Path]]:
    test_paths = [p for p in paths if is_test_requirements_file(p)]
    product_paths = [p for p in paths if p not in test_paths]
    return product_paths, test_paths


def load_workspace_test_requirements(workspace: Path) -> str | None:
    """加载工作区 input/test-requirements.md（若存在）。"""
    for name in TEST_REQUIREMENTS_NAMES:
        path = workspace / "input" / name
        if path.is_file():
            return read_document(path).strip()
    return None


def merge_documents(
    paths: list[Path],
    test_requirements_text: str | None = None,
) -> str:
    """合并产品需求文档；测试需求单独成章置于最前。"""
    product_paths, test_paths = split_document_paths(paths)

    test_sections: list[str] = []
    if test_requirements_text:
        test_sections.append(test_requirements_text.strip())
    for path in test_paths:
        test_sections.append(f"### 来源: {path.name}\n\n{read_document(path).strip()}")

    body_parts: list[str] = ["# 合并需求文档\n"]
    if test_sections:
        body_parts.append(
            "## 测试需求（测试范围与重点，生成时优先遵循）\n\n"
            + "\n\n---\n\n".join(test_sections)
            + "\n"
        )

    body_parts.append("## 产品需求文档\n")
    if not product_paths:
        if not test_sections:
            raise ValueError("至少需要一份产品需求或测试需求文档")
    else:
        for path in product_paths:
            content = read_document(path).strip()
            if content:
                body_parts.append(f"\n---\n\n### 文档: {path.name}\n\n{content}\n")

    merged = "".join(body_parts).strip()
    if not merged:
        raise ValueError("所有文档内容为空")
    return merged


def ingest(
    source: Path,
    compiled_path: Path,
    workspace: Path | None = None,
) -> IngestResult:
    """摄入文档并写入 compiled 文件。"""
    paths = collect_documents(source)
    product_paths, test_paths = split_document_paths(paths)

    extra_test = None
    if workspace:
        extra_test = load_workspace_test_requirements(workspace)

    test_parts = []
    if extra_test:
        test_parts.append(extra_test)
    for p in test_paths:
        test_parts.append(read_document(p).strip())
    test_text = "\n\n".join(test_parts) if test_parts else None

    merged = merge_documents(paths, test_requirements_text=extra_test)
    compiled_path.parent.mkdir(parents=True, exist_ok=True)
    compiled_path.write_text(merged, encoding="utf-8")

    return IngestResult(
        requirement_text=merged,
        test_requirements_text=test_text,
        product_paths=product_paths,
        test_requirements_paths=test_paths,
        compiled_path=compiled_path,
    )


# 向后兼容
def merge_documents_legacy(paths: list[Path]) -> str:
    return merge_documents(paths)
