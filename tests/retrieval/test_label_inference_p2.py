from __future__ import annotations

from rag_pipeline.retrieval.label_inference import is_valid_label


def test_allowed_labels_respected():
    allowed = ["Recipe", "Ingredient"]
    assert is_valid_label("recipe", allowed) is True
    assert is_valid_label("Product", allowed) is False
