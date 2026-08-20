"""LLM 客户端重试/退避行为测试（不发起真实网络请求）。

测试用 API Key 通过环境变量注入（明显为桩值，非真实凭据）。
"""

from __future__ import annotations

import io
import json
import os
import urllib.error

import pytest

from qagent.agent.llm import OpenAILLM
from qagent.config import LLMConfig


@pytest.fixture(autouse=True)
def _stub_api_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", os.environ.get("OPENAI_API_KEY") or "qagent-test-stub")


def _ok_response() -> io.BytesIO:
    return io.BytesIO(
        json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode("utf-8")
    )


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://llm.test/v1/chat/completions", code, "err", None,
        io.BytesIO(b"{}"),
    )


def _llm(**kwargs) -> OpenAILLM:
    params = {"retries": 2, "backoff_seconds": 0}
    params.update(kwargs)
    return OpenAILLM(LLMConfig(**params))


def test_retry_on_429_then_success(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(timeout)
        if len(calls) == 1:
            raise _http_error(429)
        return _ok_response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert _llm().complete("s", "u") == "ok"
    assert len(calls) == 2
    assert calls[0] > 0  # timeout 来自配置且生效


def test_retry_on_5xx_then_success(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(1)
        if len(calls) <= 2:
            raise _http_error(503)
        return _ok_response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert _llm().complete("s", "u") == "ok"
    assert len(calls) == 3


def test_no_retry_on_4xx_client_error(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(1)
        raise _http_error(401)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="401"):
        _llm().complete("s", "u")
    assert len(calls) == 1


def test_retry_exhausted_raises_last_error(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(1)
        raise _http_error(503)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="503"):
        _llm().complete("s", "u")
    assert len(calls) == 3  # 1 次原始 + 2 次重试


def test_network_error_retries(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.URLError("connection reset")
        return _ok_response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert _llm().complete("s", "u") == "ok"
    assert len(calls) == 2


def test_zero_retries_behaves_like_before(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(1)
        raise _http_error(429)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="429"):
        _llm(retries=0).complete("s", "u")
    assert len(calls) == 1
