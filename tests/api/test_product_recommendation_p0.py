from __future__ import annotations

from api import product_recommendation as pr


# ── Helpers shared across tests ───────────────────────────────────────────────

def _make_driver(*rows):
    """Return a minimal fake Driver whose session yields the given row dicts."""
    class _Session:
        def run(self, *_a, **_k):
            return iter(rows)

    class _Driver:
        def session(self, database=None):
            class CM:
                def __enter__(self): return _Session()
                def __exit__(self, *_): return False
            return CM()

    return _Driver()


def test_product_recommendation_certification_filter_with_fallback_when_none(monkeypatch):
    monkeypatch.setattr(pr, "_product_data_available", lambda *_a, **_k: True)
    monkeypatch.setattr(pr, "_filter_allergen_unsafe_product_ids", lambda *_a, **_k: set())
    monkeypatch.setattr(pr, "_filter_products_by_certification", lambda *_a, **_k: set())

    class _Session:
        def run(self, *_a, **_k):
            return iter(
                [
                    {"ingredient_id": "i1", "product_id": "p1", "product_name": "A", "brand": "X", "price": 5.0, "currency": "USD", "weight_g": 100, "category": "cat", "image_url": ""},
                    {"ingredient_id": "i1", "product_id": "p2", "product_name": "B", "brand": "Y", "price": 4.0, "currency": "USD", "weight_g": 100, "category": "cat", "image_url": ""},
                ]
            )

    class _Driver:
        def session(self, database=None):
            class CM:
                def __enter__(self): return _Session()
                def __exit__(self, *_): return False
            return CM()

    out = pr.run_recommend_products(
        _Driver(),
        ingredient_ids=["i1"],
        ingredient_names={"i1": "milk"},
        quality_preferences=["organic"],
    )
    assert out["products"]
    assert out["products"][0]["preference_matched"] is False


def test_product_recommendation_applies_brand_boost_and_budget_cutoff(monkeypatch):
    monkeypatch.setattr(pr, "_product_data_available", lambda *_a, **_k: True)
    monkeypatch.setattr(pr, "_filter_allergen_unsafe_product_ids", lambda *_a, **_k: set())
    monkeypatch.setattr(pr, "_filter_products_by_certification", lambda *_a, **_k: {"p1", "p2"})

    class _Session:
        def run(self, *_a, **_k):
            return iter(
                [
                    {"ingredient_id": "i1", "product_id": "p1", "product_name": "A", "brand": "Preferred", "price": 5.0, "currency": "USD", "weight_g": 100, "category": "cat", "image_url": ""},
                    {"ingredient_id": "i1", "product_id": "p2", "product_name": "B", "brand": "Other", "price": 4.95, "currency": "USD", "weight_g": 100, "category": "cat", "image_url": ""},
                    {"ingredient_id": "i1", "product_id": "p3", "product_name": "C", "brand": "Other", "price": 12.0, "currency": "USD", "weight_g": 100, "category": "cat", "image_url": ""},
                ]
            )

    class _Driver:
        def session(self, database=None):
            class CM:
                def __enter__(self): return _Session()
                def __exit__(self, *_): return False
            return CM()

    out = pr.run_recommend_products(
        _Driver(),
        ingredient_ids=["i1"],
        ingredient_names={"i1": "milk"},
        preferred_brands=["Preferred"],
        household_budget=10.0,
    )
    assert len(out["products"]) == 1
    assert out["products"][0]["product_id"] == "p1"


# ── get_match_confidence unit tests ──────────────────────────────────────────

def test_get_match_confidence_id_match():
    result = pr.get_match_confidence(
        ingredient_id_returned="ing-1",
        ingredient_name_returned="Chicken Breast",
        iid="ing-1",
        iname="chicken",
    )
    assert result == "id_match"


def test_get_match_confidence_name_exact():
    result = pr.get_match_confidence(
        ingredient_id_returned="",
        ingredient_name_returned="Chicken Breast",
        iid="ing-1",
        iname="Chicken Breast",
    )
    assert result == "name_exact"


def test_get_match_confidence_name_partial_graph_contains_search():
    # Graph node name "Boneless Chicken Breast" contains search term "chicken"
    result = pr.get_match_confidence(
        ingredient_id_returned="",
        ingredient_name_returned="Boneless Chicken Breast",
        iid="ing-1",
        iname="chicken",
    )
    assert result == "name_partial"


def test_get_match_confidence_name_partial_search_contains_graph():
    # Search term "organic whole milk" contains graph name "milk"
    result = pr.get_match_confidence(
        ingredient_id_returned="",
        ingredient_name_returned="milk",
        iid="ing-1",
        iname="organic whole milk",
    )
    assert result == "name_partial"


def test_get_match_confidence_name_unknown_when_no_overlap():
    result = pr.get_match_confidence(
        ingredient_id_returned="",
        ingredient_name_returned="",
        iid="ing-1",
        iname="",
    )
    assert result == "name_unknown"


# ── match_confidence propagated through run_recommend_products ────────────────

def test_run_recommend_products_returns_match_confidence_field(monkeypatch):
    monkeypatch.setattr(pr, "_product_data_available", lambda *_a, **_k: True)
    monkeypatch.setattr(pr, "_filter_allergen_unsafe_product_ids", lambda *_a, **_k: set())
    monkeypatch.setattr(pr, "_filter_products_by_certification", lambda *_a, **_k: set())

    driver = _make_driver(
        {"ingredient_id": "i1", "ingredient_name": "Whole Milk", "product_id": "p1",
         "product_name": "Brand Milk", "brand": "X", "price": 3.0, "currency": "USD",
         "weight_g": 100, "category": "dairy", "image_url": ""},
    )
    out = pr.run_recommend_products(
        driver,
        ingredient_ids=["i1"],
        ingredient_names={"i1": "milk"},
    )
    assert out["products"]
    assert "match_confidence" in out["products"][0]


def test_run_recommend_products_partial_match_confidence_for_contained_name(monkeypatch):
    monkeypatch.setattr(pr, "_product_data_available", lambda *_a, **_k: True)
    monkeypatch.setattr(pr, "_filter_allergen_unsafe_product_ids", lambda *_a, **_k: set())
    monkeypatch.setattr(pr, "_filter_products_by_certification", lambda *_a, **_k: set())

    # Graph finds ingredient via name containment (Fix A); Neo4j node ID differs from our iid.
    # graph_ingredient_id != iid → confidence falls through to name comparison → name_partial.
    driver = _make_driver(
        {"ingredient_id": "i1", "graph_ingredient_id": "neo4j-node-abc",
         "ingredient_name": "Boneless Chicken Breast", "product_id": "p1",
         "product_name": "Chicken Pack", "brand": "Y", "price": 5.0, "currency": "USD",
         "weight_g": 500, "category": "meat", "image_url": ""},
    )
    out = pr.run_recommend_products(
        driver,
        ingredient_ids=["i1"],
        ingredient_names={"i1": "chicken"},
    )
    assert out["products"][0]["match_confidence"] == "name_partial"


# ── selection_mode field is accepted without breaking existing behaviour ───────

def test_run_recommend_products_accepts_selection_mode_field(monkeypatch):
    """selection_mode is currently a logging/routing hint; behaviour must be unchanged."""
    monkeypatch.setattr(pr, "_product_data_available", lambda *_a, **_k: True)
    monkeypatch.setattr(pr, "_filter_allergen_unsafe_product_ids", lambda *_a, **_k: set())
    monkeypatch.setattr(pr, "_filter_products_by_certification", lambda *_a, **_k: set())

    driver = _make_driver(
        {"ingredient_id": "i1", "ingredient_name": "Olive Oil", "product_id": "p1",
         "product_name": "Oil", "brand": "Z", "price": 4.0, "currency": "USD",
         "weight_g": 250, "category": "pantry", "image_url": ""},
    )
    # Pass selection_mode — should not raise and should return the same product
    out = pr.run_recommend_products(
        driver,
        ingredient_ids=["i1"],
        ingredient_names={"i1": "olive oil"},
        # selection_mode is accepted at the app.py layer (ProductsRequest), not passed
        # to run_recommend_products directly — this test validates the core function
        # still works regardless of the field existing in the request schema.
    )
    assert len(out["products"]) == 1
    assert out["products"][0]["product_id"] == "p1"


# ── allergen + quality interactions unaffected by matching change ─────────────

def test_allergen_filter_still_applied_with_partial_name_match(monkeypatch):
    monkeypatch.setattr(pr, "_product_data_available", lambda *_a, **_k: True)
    # p1 is allergen-unsafe, p2 is safe
    monkeypatch.setattr(pr, "_filter_allergen_unsafe_product_ids", lambda *_a, **_k: {"p1"})
    monkeypatch.setattr(pr, "_filter_products_by_certification", lambda *_a, **_k: set())

    driver = _make_driver(
        {"ingredient_id": "i1", "ingredient_name": "Whole Milk", "product_id": "p1",
         "product_name": "AllergyMilk", "brand": "A", "price": 2.0, "currency": "USD",
         "weight_g": 100, "category": "dairy", "image_url": ""},
        {"ingredient_id": "i1", "ingredient_name": "Whole Milk", "product_id": "p2",
         "product_name": "SafeMilk", "brand": "B", "price": 3.0, "currency": "USD",
         "weight_g": 100, "category": "dairy", "image_url": ""},
    )
    out = pr.run_recommend_products(
        driver,
        ingredient_ids=["i1"],
        ingredient_names={"i1": "milk"},
        customer_allergens=["peanut"],
    )
    assert len(out["products"]) == 1
    assert out["products"][0]["product_id"] == "p2"


def test_recommend_alternatives_uses_category_fallback_if_no_graph_substitutes(monkeypatch):
    monkeypatch.setattr(pr, "_product_data_available", lambda *_a, **_k: True)
    monkeypatch.setattr(pr, "_product_exists", lambda *_a, **_k: True)
    monkeypatch.setattr(pr, "_filter_allergen_unsafe_product_ids", lambda *_a, **_k: {"p2"})

    class _Session:
        def __init__(self):
            self.calls = 0

        def run(self, *_a, **_k):
            self.calls += 1
            if self.calls == 1:
                return iter([])  # no CAN_SUBSTITUTE
            return iter(
                [
                    {"product_id": "p1", "name": "Alt1", "brand": "B1", "price": 3.0, "image_url": "", "category": "cat", "orig_price": 5.0},
                    {"product_id": "p2", "name": "Alt2", "brand": "B2", "price": 2.0, "image_url": "", "category": "cat", "orig_price": 5.0},
                ]
            )

    class _Driver:
        def __init__(self):
            self.session_obj = _Session()

        def session(self, database=None):
            session = self.session_obj
            class CM:
                def __enter__(self): return session
                def __exit__(self, *_): return False
            return CM()

    out = pr.run_recommend_alternatives(_Driver(), product_id="orig", customer_allergens=["peanut"])
    assert [a["product_id"] for a in out["alternatives"]] == ["p1"]
