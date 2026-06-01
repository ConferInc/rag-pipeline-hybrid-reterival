from __future__ import annotations

from rag_pipeline.augmentation.condense import condense_for_llm


def test_condense_formats_and_truncates_long_list():
    expanded = [
        {"connected_id": "1", "connected_labels": ["Recipe"], "relationship": "VIEWED", "payload": {"title": "A", "description": "x"}},
        {"connected_id": "2", "connected_labels": ["Recipe"], "relationship": "SAVED", "payload": {"title": "B", "description": "y"}},
    ]
    out = condense_for_llm(expanded, max_items=1)
    assert len(out) == 1
    assert out[0]["label"] == "Recipe"


def test_condense_empty_results():
    assert condense_for_llm([]) == []
