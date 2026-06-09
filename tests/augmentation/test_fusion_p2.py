from __future__ import annotations

from rag_pipeline.augmentation.fusion import apply_rrf
from rag_pipeline.retrieval.types import RetrievalResult


def test_rrf_empty_sources():
    out = apply_rrf([], {}, [], "find_recipe")
    assert out == []


def test_rrf_merges_overlapping_ids():
    sem = [
        RetrievalResult("r1", "Recipe", 0.9, "semantic", "idx", {"id": "r1", "title": "A", "meal_type": "dinner"}),
        RetrievalResult("r2", "Recipe", 0.8, "semantic", "idx", {"id": "r2", "title": "B", "meal_type": "dinner"}),
    ]
    struct = {"expanded_context": [{"connected_id": "r1", "connected_labels": ["Recipe"], "relationship": "SAVED", "payload": {"id": "r1", "title": "A", "meal_type": "dinner"}}]}
    out = apply_rrf(sem, struct, [], "find_recipe")
    merged = [x for x in out if "semantic" in x["sources"] and "structural" in x["sources"]]
    assert merged
