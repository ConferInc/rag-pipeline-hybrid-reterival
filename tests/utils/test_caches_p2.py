from __future__ import annotations

from rag_pipeline.intent_cache import IntentCache, get_intent_cache
from rag_pipeline.label_cache import LabelCache, get_label_cache


def test_intent_cache_lru_eviction_and_key_normalization():
    c = IntentCache(max_size=1, key_normalize="strip_lower")
    c.put(" Hello ", '{"a":1}')
    assert c.get("hello") == '{"a":1}'
    c.put("world", '{"b":1}')
    assert c.get("hello") is None


def test_label_cache_disabled_returns_none(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("label_cache:\n  enabled: false\n")
    assert get_label_cache(p) is None


def test_intent_cache_enabled_returns_instance(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("intent_cache:\n  enabled: true\n  max_size: 2\n")
    assert get_intent_cache(p) is not None
    assert isinstance(LabelCache(max_size=2), LabelCache)
