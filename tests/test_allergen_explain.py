"""PRD-40 Phase 0a — /allergens/explain + HAS_ALLERGEN edge fix (defensive units)."""

from api.product_recommendation import run_explain_allergens, _filter_allergen_unsafe_product_ids


class _FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def run(self, *_a, **_k):
        raise RuntimeError("no db")


class _FakeDriver:
    def session(self, **_k):
        return _FakeSession()


def test_explain_empty_product_id():
    assert run_explain_allergens(_FakeDriver(), product_id="") == {"allergens": []}


def test_explain_defensive_on_db_error():
    assert run_explain_allergens(_FakeDriver(), product_id="p1", allergen_codes=["PEANUT"]) == {"allergens": []}


def test_filter_still_backward_compatible_empty():
    # No allergens → {} without touching the driver (regression guard).
    assert _filter_allergen_unsafe_product_ids(_FakeDriver(), ["p1"], []) == {}


def test_cypher_uses_has_allergen_alternation():
    # Guard the safety-critical edge fix: both relationship names must be matched.
    import inspect
    from api import product_recommendation as pr

    src = inspect.getsource(pr)
    assert "HAS_ALLERGEN|CONTAINS_ALLERGEN" in src
