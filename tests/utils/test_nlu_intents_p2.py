from __future__ import annotations

from rag_pipeline.nlu.intents import VALID_INTENTS, VALID_INTENTS_WITH_B2B


def test_intent_sets_contain_expected_values():
    assert "find_recipe" in VALID_INTENTS
    assert "b2b_products_for_diet" in VALID_INTENTS_WITH_B2B
