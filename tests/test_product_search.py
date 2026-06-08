"""
PRD-40 Phase 1 — pure-logic tests for the product-search orchestrator.

Follows the codebase convention of pure-logic tests (no Neo4j mocking): the
candidate→item assembly, member-allergen mapping, and degradation paths are
exercised directly. Driver-touching Cypher (semantic vector search, allergen
graph traversal) is verified via integration/runbook, not here.
"""

from api.product_search import (
    assemble_product_items,
    resolve_member_allergen_map,
    run_search_products,
    _dedupe_preserve_order,
)
from rag_pipeline.retrieval.product_hybrid import semantic_search_products


# ── resolve_member_allergen_map ──────────────────────────────────────────────

def test_member_map_accepts_ids_names_and_codes():
    members = [
        {"id": "m1", "allergen_ids": ["PEANUT"]},
        {"id": "m2", "allergens": ["Tree Nut"]},
        {"customer_id": "m3", "allergen_codes": ["MILK"]},
    ]
    out = resolve_member_allergen_map(members)
    assert out == {"m1": ["PEANUT"], "m2": ["Tree Nut"], "m3": ["MILK"]}


def test_member_map_skips_entries_without_id_and_handles_empty():
    assert resolve_member_allergen_map(None) == {}
    assert resolve_member_allergen_map([{"allergens": ["x"]}]) == {}  # no id → skipped


# ── assemble_product_items ───────────────────────────────────────────────────

def test_seed_only_safe_item():
    items = assemble_product_items(["p1"], ["p1"], [], {}, {})
    assert items == [{
        "product_id": "p1",
        "semantic_score": None,
        "safety": "safe",
        "matching_allergens": [],
        "affected_members": [],
        "match_source": "seed",
    }]


def test_unsafe_seed_flags_warning_and_affected_members_by_code_or_name():
    unsafe = {"p1": {"matching_allergens": ["Peanut"], "allergen_codes": ["PEANUT"]}}
    members = {
        "lucas": ["PEANUT"],        # matches by code
        "mia": ["peanut"],          # matches by name (case-insensitive)
        "rose": ["MILK"],           # no match
    }
    items = assemble_product_items(["p1"], ["p1"], [], unsafe, members)
    item = items[0]
    assert item["safety"] == "warning"
    assert item["matching_allergens"] == ["Peanut"]
    assert sorted(item["affected_members"]) == ["lucas", "mia"]


def test_unsafe_without_member_map_has_empty_affected_members():
    unsafe = {"p1": {"matching_allergens": ["Peanut"], "allergen_codes": ["PEANUT"]}}
    items = assemble_product_items(["p1"], ["p1"], [], unsafe, {})
    assert items[0]["safety"] == "warning"
    assert items[0]["affected_members"] == []


def test_semantic_only_item_carries_score_and_source():
    sem = [{"product_id": "p9", "score": 0.83}]
    items = assemble_product_items(["p9"], [], sem, {}, {})
    assert items[0]["match_source"] == "semantic"
    assert items[0]["semantic_score"] == 0.83


def test_both_source_when_seed_and_semantic_overlap():
    sem = [{"product_id": "p1", "score": 0.5}]
    items = assemble_product_items(["p1"], ["p1"], sem, {}, {})
    assert items[0]["match_source"] == "both"
    assert items[0]["semantic_score"] == 0.5


def test_item_order_follows_all_ids():
    sem = [{"product_id": "p2", "score": 0.7}]
    items = assemble_product_items(["p1", "p2"], ["p1"], sem, {}, {})
    assert [i["product_id"] for i in items] == ["p1", "p2"]
    assert items[0]["match_source"] == "seed"
    assert items[1]["match_source"] == "semantic"


# ── backward-compat: dict membership filters exactly like the old set ─────────

def test_unsafe_dict_membership_filters_like_a_set():
    # Guards the two existing /recommend callers, which do `pid not in unsafe`.
    unsafe = {"p2": {"matching_allergens": ["Peanut"], "allergen_codes": ["PEANUT"]}}
    candidates = [{"product_id": "p1"}, {"product_id": "p2"}, {"product_id": "p3"}]
    kept = [c for c in candidates if c["product_id"] not in unsafe]
    assert [c["product_id"] for c in kept] == ["p1", "p3"]


# ── _dedupe_preserve_order ───────────────────────────────────────────────────

def test_dedupe_preserves_first_occurrence_order_and_drops_falsy():
    assert _dedupe_preserve_order(["a", "b", "a", "", "c", "b"]) == ["a", "b", "c"]


# ── semantic_search_products degradation (defensive, no real Neo4j) ───────────

class _Embedder:
    def embed_query(self, text):
        return [0.1, 0.2, 0.3]


class _BadEmbedder:
    def embed_query(self, text):
        raise RuntimeError("embedding provider down")


class _RaisingDriver:
    """Stands in for a Neo4j driver whose session/query fails (e.g. missing index)."""
    def session(self, **kwargs):
        raise RuntimeError("vector index not found")


def test_semantic_returns_empty_without_query_or_embedder():
    assert semantic_search_products(_RaisingDriver(), _Embedder(), "") == []
    assert semantic_search_products(_RaisingDriver(), None, "milk") == []


def test_semantic_returns_empty_when_embedder_fails():
    assert semantic_search_products(_RaisingDriver(), _BadEmbedder(), "milk") == []


def test_semantic_returns_empty_when_query_fails():
    # Embedder succeeds, but the driver/index call raises → degrade to [].
    assert semantic_search_products(_RaisingDriver(), _Embedder(), "oat milk") == []


# ── run_search_products wiring (annotate-only, no allergens → no driver use) ──

def test_run_search_products_annotate_only_no_allergens_does_not_touch_driver():
    # With annotate_only and an empty allergen union, neither semantic search nor
    # the allergen traversal touch the driver, so a sentinel object is safe.
    sentinel = object()
    result = run_search_products(
        sentinel,  # type: ignore[arg-type]
        seed_product_ids=["p1", "p2", "p1"],  # dupe collapses
        annotate_only=True,
        customer_allergens=[],
    )
    assert result["query_interpretation"] is None
    assert [p["product_id"] for p in result["products"]] == ["p1", "p2"]
    assert all(p["safety"] == "safe" and p["match_source"] == "seed" for p in result["products"])


def test_run_search_products_empty_request_returns_empty():
    sentinel = object()
    result = run_search_products(sentinel, seed_product_ids=[], annotate_only=True)  # type: ignore[arg-type]
    assert result == {"products": [], "query_interpretation": None}
