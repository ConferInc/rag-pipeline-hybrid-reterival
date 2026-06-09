from __future__ import annotations

from rag_pipeline.retrieval import service


class _Cache:
    def __init__(self, value=None):
        self.value = value
        self.writes = []

    def get(self, _query):
        return self.value

    def put(self, q, v):
        self.writes.append((q, v))


def test_infer_label_prefers_cache(monkeypatch):
    cache = _Cache("Product")
    monkeypatch.setattr(service, "get_label_cache", lambda _path: cache)
    monkeypatch.setattr(service, "_infer_label_heuristics", lambda _q: "Recipe")

    out = service.infer_label_from_query("any query", use_llm_fallback=True)
    assert out == "Product"


def test_infer_label_heuristics_then_llm_then_default(monkeypatch, tmp_path):
    cfg = tmp_path / "embedding_config.yaml"
    cfg.write_text(
        "label_inference:\n"
        "  fallback_to_llm: true\n"
        "  default_label: Recipe\n"
        "  allowed_labels: [Recipe, Ingredient, Product]\n"
    )
    cache = _Cache(None)
    monkeypatch.setattr(service, "get_label_cache", lambda _path: cache)
    monkeypatch.setattr(service, "_infer_label_heuristics", lambda _q: None)
    monkeypatch.setattr(service, "infer_label_with_llm", lambda _q, _allowed: "Ingredient")

    out = service.infer_label_from_query("complex query", config_path=str(cfg))
    assert out == "Ingredient"

    monkeypatch.setattr(service, "infer_label_with_llm", lambda _q, _allowed: None)
    out2 = service.infer_label_from_query("another query", config_path=str(cfg))
    assert out2 == "Recipe"
