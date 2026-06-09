from __future__ import annotations

import openai
import pytest

from rag_pipeline.llm_retry import with_retry


def test_with_retry_stops_after_max_attempts(monkeypatch):
    monkeypatch.setattr("rag_pipeline.llm_retry.time.sleep", lambda *_a, **_k: None)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise RuntimeError("non-retriable")

    with pytest.raises(RuntimeError):
        with_retry(fn, max_attempts=3)
    assert calls["n"] == 1


def test_with_retry_returns_result_on_eventual_success(monkeypatch):
    monkeypatch.setattr("rag_pipeline.llm_retry.time.sleep", lambda *_a, **_k: None)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 2:
            raise openai.APIConnectionError(request=None)
        return "ok"

    assert with_retry(fn, max_attempts=3) == "ok"
