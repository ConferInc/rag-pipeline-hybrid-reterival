"""
PRD-40.1 Phase 4 — `exclude_ids` on /recommend/alternatives.

The Cypher-level exclusion is verified via integration; here we guard the
contract: the new optional param is accepted and the function stays
backward-compatible (defensive empty result when product data is unavailable),
matching the codebase's pure-test convention (no live Neo4j).
"""

from api.product_recommendation import run_recommend_alternatives


class _FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def run(self, *_a, **_k):
        raise RuntimeError("no db")

    def single(self):
        return None


class _FakeDriver:
    def session(self, **_k):
        return _FakeSession()


def test_accepts_exclude_ids_and_higher_limit_without_error():
    out = run_recommend_alternatives(
        _FakeDriver(),
        product_id="p1",
        customer_allergens=["PEANUT"],
        limit=40,
        exclude_ids=["p1", "p2"],
    )
    assert out == {"alternatives": []}


def test_backward_compatible_without_exclude_ids():
    out = run_recommend_alternatives(_FakeDriver(), product_id="p1")
    assert out == {"alternatives": []}


def test_empty_product_id_returns_empty():
    out = run_recommend_alternatives(_FakeDriver(), product_id="", exclude_ids=["x"])
    assert out == {"alternatives": []}
