from __future__ import annotations

from rag_pipeline.orchestrator.entity_validation import validate_entity_compatibility


def test_rejects_conflicting_diet_ingredient():
    out = validate_entity_compatibility(
        {"diet": ["Vegan"], "include_ingredient": ["chicken", "tofu"]}
    )
    assert "chicken" not in out["include_ingredient"]
    assert "tofu" in out["include_ingredient"]


def test_multiple_conflicts_all_reported():
    out = validate_entity_compatibility(
        {"diet": ["Gluten-Free", "Keto"], "include_ingredient": ["bread", "sugar", "eggs"]}
    )
    assert "bread" not in out["include_ingredient"]
    assert "sugar" not in out["include_ingredient"]
