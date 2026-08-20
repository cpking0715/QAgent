"""LLM 客户端：OpenAI 兼容 API + Mock（测试用）。

出站请求策略：
- 429/5xx/网络错误按指数退避重试（次数与基数见 LLMConfig，qagent.yaml llm 段可调）；
- 4xx（鉴权/参数类）不重试，直接抛错；
- 全局并发上限由环境变量 QAGENT_MAX_CONCURRENT_LLM 控制（默认 16），
  覆盖"多任务 × 多批次"嵌套并发，避免打爆网关。
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Protocol

from qagent.config import LLMConfig

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class LLMCancelled(Exception):
    """LLM 流式输出期间收到终止请求。"""


_GATE_LOCK = threading.Lock()
_GATE: threading.BoundedSemaphore | None = None


def _llm_gate() -> threading.BoundedSemaphore:
    global _GATE
    with _GATE_LOCK:
        if _GATE is None:
            try:
                size = int(os.environ.get("QAGENT_MAX_CONCURRENT_LLM", "16"))
            except ValueError:
                size = 16
            _GATE = threading.BoundedSemaphore(max(1, size))
        return _GATE


class LLMClient(Protocol):
    def complete(self, system: str, user: str) -> str: ...


class OpenAILLM:
    """OpenAI 兼容 Chat Completions（支持 OpenAI / Azure / 本地 vLLM）。"""

    def __init__(self, config: LLMConfig, api_key: str | None = None) -> None:
        self.config = config
        self.api_key = config.resolve_api_key(api_key)
        # 可由服务层注入：流式读取的每个 chunk 之间检查，实现秒级取消
        self.should_cancel: "Callable[[], bool] | None" = None

    def _complete_once(self, system: str, user: str) -> str:
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
        if self.config.stream:
            payload["stream"] = True
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
            with urllib.request.urlopen(
                request, timeout=self.config.timeout,
            ) as response:
                if self.config.stream:
                    return self._read_stream(response)
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"LLM API 错误 ({exc.code}): {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"LLM 网络错误: {exc.reason}") from exc

        try:
            return body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"LLM 响应格式异常: {body}") from exc

    def _read_stream(self, response) -> str:
        """逐行读取 SSE 流并聚合 content；chunk 间检查取消，实现秒级终止。"""
        parts: list[str] = []
        for raw_line in response:
            if self.should_cancel is not None and self.should_cancel():
                raise LLMCancelled("用户终止")
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            try:
                content = chunk["choices"][0]["delta"].get("content")
            except (KeyError, IndexError, TypeError):
                continue
            if content:
                parts.append(content)
        return "".join(parts)

    def complete(self, system: str, user: str) -> str:
        """单次请求走 _complete_once；可重试错误按指数退避重试，全局信号量限流。"""
        retries = max(0, self.config.retries)
        for attempt in range(retries + 1):
            try:
                with _llm_gate():
                    return self._complete_once(system, user)
            except RuntimeError as exc:
                if attempt >= retries or not _is_retryable(str(exc)):
                    raise
                time.sleep(self.config.backoff_seconds * (2 ** attempt))
        raise RuntimeError("LLM 请求未返回内容")


def _is_retryable(message: str) -> bool:
    if message.startswith("LLM 网络错误"):
        return True
    prefix = "LLM API 错误 ("
    if message.startswith(prefix):
        try:
            code = int(message[len(prefix):].split(")", 1)[0])
        except ValueError:
            return False
        return code in RETRYABLE_STATUS
    return False


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
