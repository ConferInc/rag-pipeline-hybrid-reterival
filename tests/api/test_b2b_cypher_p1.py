from __future__ import annotations

from api import b2b_cypher as bc


def test_b2b_cypher_builds_query_for_supported_intents():
    cypher, params = bc.build_b2b_products_for_diet("v1", ["vegan"], limit=5)
    assert "MATCH (p:Product)" in cypher
    assert params["vendor_id"] == "v1"
    assert params["limit"] == 5


def test_b2b_cypher_handles_unknown_condition_with_fallback():
    cypher, params = bc.build_b2b_products_for_condition("v1", ["unknown_condition"], limit=3)
    assert "MATCH (p:Product)" in cypher
    assert params["vendor_id"] == "v1"
