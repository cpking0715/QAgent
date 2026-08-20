"""共享测试 fixture：MockLLM 全流水线响应。"""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def mock_responses() -> dict[str, str]:
    return {
        "test-requirements": (FIXTURES / "test-requirements-generated.md").read_text(encoding="utf-8"),
        "test-plan": (FIXTURES / "test-plan.md").read_text(encoding="utf-8"),
        "risk.md": (FIXTURES / "risk.md").read_text(encoding="utf-8"),
        "testcases": (FIXTURES / "testcases-valid.md").read_text(encoding="utf-8"),
        "__fix__": (FIXTURES / "testcases-valid.md").read_text(encoding="utf-8"),
        "coverage-matrix": (FIXTURES / "coverage-matrix.md").read_text(encoding="utf-8"),
        "qa-review": (FIXTURES / "qa-review.md").read_text(encoding="utf-8"),
        "__fix_matrix__": (FIXTURES / "coverage-matrix.md").read_text(encoding="utf-8"),
    }
