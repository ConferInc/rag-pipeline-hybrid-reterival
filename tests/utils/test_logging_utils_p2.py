from __future__ import annotations

from rag_pipeline.logging_utils import hash_for_log, truncate_for_log


def test_truncate_for_log_and_hash_for_log():
    assert truncate_for_log("abc", max_len=5) == "abc"
    assert truncate_for_log("abcdef", max_len=3) == "abc..."
    h1 = hash_for_log("value")
    h2 = hash_for_log("value")
    assert h1 == h2
