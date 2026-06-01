from __future__ import annotations

from rag_pipeline.embeddings.caching_embedder import CachingQueryEmbedder


class D:
    def __init__(self):
        self.calls = 0

    def embed_query(self, text):
        self.calls += 1
        return [float(len(text))]


def test_cache_hit_skips_underlying_embedder():
    d = D()
    c = CachingQueryEmbedder(d, max_size=10)
    a = c.embed_query("hello")
    b = c.embed_query("hello")
    assert a == b
    assert d.calls == 1


def test_cache_lru_eviction():
    d = D()
    c = CachingQueryEmbedder(d, max_size=1)
    c.embed_query("one")
    c.embed_query("two")
    c.embed_query("one")
    assert d.calls == 3
