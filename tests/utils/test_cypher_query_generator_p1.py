from __future__ import annotations

import pytest

import cypher_query_generator as cqg


def test_generate_cypher_query_find_recipe_with_all_filters():
    cypher, params = cqg.generate_cypher_query(
        "find_recipe",
        {
            "diet": ["Vegan"],
            "course": "dinner",
            "cal_upper_limit": 600,
            "include_ingredient": ["tofu"],
        },
        limit=7,
    )
    assert "MATCH (r:Recipe)" in cypher
    assert "LIMIT 7" in cypher


def test_generate_cypher_query_unknown_intent_raises():
    with pytest.raises(ValueError):
        cqg.generate_cypher_query("unknown_intent", {})


def test_operator_mapping_defaults_safely():
    assert cqg._op("gt") == ">="
    assert cqg._op("lt") == "<="
    assert cqg._op("invalid") == ">="
