from __future__ import annotations

from api import b2b


def test_b2b_route_intent_maps_entities_to_correct_builder(monkeypatch):
    monkeypatch.setattr(b2b, "build_b2b_products_for_diet", lambda *a, **k: ("Q1", {"x": 1}))
    cypher, params = b2b.route_b2b_intent(
        "b2b_products_for_diet",
        {"diet": ["vegan"]},
        "vendor-1",
    )
    assert cypher == "Q1"
    assert params == {"x": 1}


def test_b2b_search_merges_nlu_entities_into_filters(monkeypatch):
    monkeypatch.setattr(b2b, "build_b2b_products_allergen_free", lambda *a, **k: ("Q2", {"a": 1}))
    cypher, params = b2b.route_b2b_intent(
        "b2b_products_allergen_free",
        {"exclude_ingredient": ["peanut"]},
        "vendor-1",
    )
    assert cypher == "Q2"
    assert params == {"a": 1}
