from __future__ import annotations

from rag_pipeline.retrieval.types import RetrievalResult


def test_retrieval_types_serialization_roundtrip():
    r = RetrievalResult("n1", "Recipe", 0.9, "semantic", "idx", {"id": "n1"})
    d = r.to_dict()
    assert d["node_id"] == "n1"
    assert d["payload"]["id"] == "n1"
