"""LLM 客户端：OpenAI 兼容 API + Mock（测试用）。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Protocol

from qagent.config import LLMConfig


class LLMClient(Protocol):
    def complete(self, system: str, user: str) -> str: ...


class OpenAILLM:
    """OpenAI 兼容 Chat Completions（支持 OpenAI / Azure / 本地 vLLM）。"""

    def __init__(self, config: LLMConfig, api_key: str | None = None) -> None:
        self.config = config
        self.api_key = config.resolve_api_key(api_key)

    def complete(self, system: str, user: str) -> str:
        if not self.api_key:
            raise RuntimeError(
                "未配置 LLM API Key。请复制 qagent.local.yaml.example 为 qagent.local.yaml "
                f"并填写 llm.api_key，或设置环境变量 {self.config.api_key_env}"
            )
        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": self.config.model,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"LLM API 错误 ({exc.code}): {detail}") from exc

        try:
            return body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"LLM 响应格式异常: {body}") from exc


class MockLLM:
    """测试用 Mock：按 prompt 关键词返回 fixture 内容。"""

    def __init__(self, responses: dict[str, str] | None = None) -> None:
        self._responses = responses or {}
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if "矩阵结构无效" in user:
            return self._responses.get("__fix_matrix__", self._responses.get("coverage-matrix", ""))
        if "修正" in user or "校验失败" in user:
            return self._responses.get("__fix__", self._responses.get("testcases", ""))

        task_markers = [
            ("生成完整的 coverage-matrix.md", "coverage-matrix"),
            ("生成完整的 qa-review.md", "qa-review"),
            ("生成完整的 testcases.md", "testcases"),
            ("生成完整的 risk.md", "risk.md"),
            ("生成完整的 test-plan.md", "test-plan"),
            ("生成完整的 test-requirements.md", "test-requirements"),
        ]
        for phrase, key in task_markers:
            if phrase in user and key in self._responses:
                return self._responses[key]

        for keyword, content in sorted(
            self._responses.items(), key=lambda item: len(item[0]), reverse=True,
        ):
            if keyword.startswith("__"):
                continue
            if keyword in user or keyword in system:
                return content
        raise KeyError(f"MockLLM 无匹配响应，user 前 80 字: {user[:80]!r}")
