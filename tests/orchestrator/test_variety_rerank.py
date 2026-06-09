"""
Tier 3 PR 1 — Multi-day variety reranking tests.

Covers:
  infer_protein_source:
    - Returns canonical label from known title keywords
    - Returns None when no match
    - Case-insensitive matching

  profile_enrichment.merge_profile_into_entities:
    - recentCuisines  → entities["recent_cuisines"]
    - recentProteinSources → entities["recent_protein_sources"]

  constraint_filter.contextual_rerank:
    - Cuisine consecutive penalty: same cuisine in last 2 entries → ×0.6
    - Protein rotation penalty: title-inferred protein in recent list → ×0.7
    - No penalty when signals are absent (backward compat)
    - Cuisine penalty does NOT fire when last 2 cuisines differ
    - Signals are independent — both can stack
"""

from __future__ import annotations

import pytest

from rag_pipeline.orchestrator.constraint_filter import (
    contextual_rerank,
    infer_protein_source,
)
from rag_pipeline.orchestrator.profile_enrichment import merge_profile_into_entities


# ── Helpers ────────────────────────────────────────────────────────────────────


def _item(rid: str, title: str | None = None, cuisine: str | None = None, score: float = 1.0) -> dict:
    payload: dict = {"id": rid, "title": title or f"Recipe {rid}"}
    if cuisine:
        payload["cuisine_code"] = cuisine
    return {"payload": payload, "rrf_score": score, "score": score, "sources": []}


def _rerank(items, entities):
    return contextual_rerank(items, entities)


def _scores(result):
    return {r["payload"]["id"]: r["score"] for r in result}


# ── infer_protein_source ──────────────────────────────────────────────────────


def test_infer_chicken_from_title():
    assert infer_protein_source("Chicken Tikka Masala") == "chicken"


def test_infer_beef_from_title():
    assert infer_protein_source("Beef Stir Fry") == "beef"


def test_infer_fish_from_salmon():
    assert infer_protein_source("Grilled Salmon with Lemon") == "fish"


def test_infer_fish_from_tuna():
    assert infer_protein_source("Tuna Pasta Bake") == "fish"


def test_infer_shellfish_from_shrimp():
    assert infer_protein_source("Shrimp Fried Rice") == "shellfish"


def test_infer_shellfish_from_prawn():
    assert infer_protein_source("Prawn Curry") == "shellfish"


def test_infer_lamb_from_title():
    assert infer_protein_source("Slow Cooked Lamb Shank") == "lamb"


def test_infer_pork_from_title():
    assert infer_protein_source("Pork Belly Ramen") == "pork"


def test_infer_legume_from_lentil():
    assert infer_protein_source("Red Lentil Dal") == "legume"


def test_infer_legume_from_chickpea():
    assert infer_protein_source("Chickpea Curry") == "legume"


def test_infer_tofu_from_title():
    assert infer_protein_source("Crispy Tofu Bowl") == "tofu"


def test_infer_egg_from_title():
    assert infer_protein_source("Spinach and Egg Frittata") == "egg"


def test_infer_paneer_from_title():
    assert infer_protein_source("Palak Paneer") == "paneer"


def test_infer_none_for_vegetable_dish():
    assert infer_protein_source("Roasted Vegetable Medley") is None


def test_infer_none_for_empty_title():
    assert infer_protein_source("") is None


def test_infer_none_for_generic_title():
    assert infer_protein_source("Quick Salad") is None


def test_infer_case_insensitive():
    assert infer_protein_source("CHICKEN SOUP") == "chicken"
    assert infer_protein_source("grilled salmon") == "fish"


def test_infer_first_match_wins():
    # "beef" and "lamb" both present; beef appears first in _PROTEIN_KEYWORDS
    result = infer_protein_source("Beef and Lamb Stew")
    assert result in ("beef", "lamb")  # one of them, not None


# ── merge_profile_into_entities: variety signal threading ─────────────────────


def test_recent_cuisines_threaded_into_entities():
    profile = {"context": {"recentCuisines": ["indian", "italian"]}}
    result = merge_profile_into_entities({}, profile)
    assert result["recent_cuisines"] == ["indian", "italian"]


def test_recent_cuisines_normalised_to_lowercase():
    profile = {"context": {"recentCuisines": ["Indian", "ITALIAN"]}}
    result = merge_profile_into_entities({}, profile)
    assert result["recent_cuisines"] == ["indian", "italian"]


def test_recent_protein_sources_threaded_into_entities():
    profile = {"context": {"recentProteinSources": ["chicken", "beef"]}}
    result = merge_profile_into_entities({}, profile)
    assert result["recent_protein_sources"] == ["chicken", "beef"]


def test_recent_protein_sources_normalised_to_lowercase():
    profile = {"context": {"recentProteinSources": ["Chicken", "BEEF"]}}
    result = merge_profile_into_entities({}, profile)
    assert result["recent_protein_sources"] == ["chicken", "beef"]


def test_missing_variety_context_does_not_add_keys():
    profile = {"context": {"cuisinePreferences": ["indian"]}}
    result = merge_profile_into_entities({}, profile)
    assert "recent_cuisines" not in result
    assert "recent_protein_sources" not in result


def test_empty_context_no_variety_keys():
    result = merge_profile_into_entities({}, {})
    assert "recent_cuisines" not in result
    assert "recent_protein_sources" not in result


# ── Cuisine consecutive penalty ────────────────────────────────────────────────


def test_cuisine_overused_penalty_fires_when_last_two_same():
    """Indian used on day 2 AND day 3 → indian candidates penalised on day 4."""
    entities = {"recent_cuisines": ["mexican", "indian", "indian"]}
    items = [
        _item("r_indian", cuisine="indian", score=1.0),
        _item("r_mexican", cuisine="mexican", score=1.0),
    ]
    scores = _scores(_rerank(items, entities))
    assert scores["r_indian"] < scores["r_mexican"]


def test_cuisine_penalty_does_not_fire_when_last_two_differ():
    entities = {"recent_cuisines": ["indian", "italian"]}
    items = [
        _item("r_indian", cuisine="indian", score=1.0),
        _item("r_italian", cuisine="italian", score=1.0),
    ]
    scores = _scores(_rerank(items, entities))
    assert scores["r_indian"] == pytest.approx(scores["r_italian"], abs=1e-6)


def test_cuisine_penalty_does_not_affect_different_cuisine():
    entities = {"recent_cuisines": ["indian", "indian"]}
    items = [
        _item("r_indian", cuisine="indian", score=1.0),
        _item("r_mexican", cuisine="mexican", score=1.0),
        _item("r_italian", cuisine="italian", score=1.0),
    ]
    scores = _scores(_rerank(items, entities))
    assert scores["r_indian"] == pytest.approx(0.6, abs=1e-6)
    assert scores["r_mexican"] == pytest.approx(1.0, abs=1e-6)
    assert scores["r_italian"] == pytest.approx(1.0, abs=1e-6)


def test_cuisine_penalty_magnitude():
    entities = {"recent_cuisines": ["indian", "indian"]}
    items = [_item("r1", cuisine="indian", score=1.0)]
    result = _rerank(items, entities)
    assert result[0]["score"] == pytest.approx(0.6, abs=1e-6)


def test_single_recent_cuisine_no_penalty():
    entities = {"recent_cuisines": ["indian"]}
    items = [_item("r1", cuisine="indian", score=1.0)]
    result = _rerank(items, entities)
    assert result[0]["score"] == pytest.approx(1.0, abs=1e-6)


def test_no_recent_cuisines_no_penalty():
    items = [_item("r1", cuisine="indian", score=1.0)]
    result = _rerank(items, {})
    assert result[0]["score"] == pytest.approx(1.0, abs=1e-6)


# ── Protein rotation penalty (title-based) ────────────────────────────────────


def test_protein_rotation_penalty_fires_from_title():
    """Chicken used on recent days → Chicken Tikka candidate penalised."""
    entities = {"recent_protein_sources": ["chicken"]}
    items = [
        _item("r_chicken", title="Chicken Tikka Masala", score=1.0),
        _item("r_fish", title="Grilled Salmon Bowl", score=1.0),
    ]
    scores = _scores(_rerank(items, entities))
    assert scores["r_chicken"] < scores["r_fish"]


def test_protein_penalty_magnitude():
    entities = {"recent_protein_sources": ["chicken"]}
    items = [_item("r1", title="Chicken Curry", score=1.0)]
    result = _rerank(items, entities)
    assert result[0]["score"] == pytest.approx(0.7, abs=1e-6)


def test_protein_penalty_no_keyword_in_title_is_noop():
    """A recipe title with no protein keyword doesn't get penalised."""
    entities = {"recent_protein_sources": ["chicken"]}
    items = [_item("r1", title="Roasted Vegetable Medley", score=1.0)]
    result = _rerank(items, entities)
    assert result[0]["score"] == pytest.approx(1.0, abs=1e-6)


def test_protein_penalty_different_protein_no_penalty():
    entities = {"recent_protein_sources": ["chicken"]}
    items = [_item("r1", title="Grilled Salmon with Herbs", score=1.0)]
    result = _rerank(items, entities)
    assert result[0]["score"] == pytest.approx(1.0, abs=1e-6)


def test_protein_penalty_legume_title():
    entities = {"recent_protein_sources": ["legume"]}
    items = [_item("r1", title="Red Lentil Dal", score=1.0)]
    result = _rerank(items, entities)
    assert result[0]["score"] == pytest.approx(0.7, abs=1e-6)


def test_no_recent_protein_sources_no_penalty():
    items = [_item("r1", title="Chicken Curry", score=1.0)]
    result = _rerank(items, {})
    assert result[0]["score"] == pytest.approx(1.0, abs=1e-6)


# ── Both signals stack ────────────────────────────────────────────────────────


def test_both_penalties_stack():
    """Recipe with overused cuisine AND repeated protein gets both penalties."""
    entities = {
        "recent_cuisines": ["indian", "indian"],
        "recent_protein_sources": ["chicken"],
    }
    items = [_item("r1", title="Chicken Tikka Masala", cuisine="indian", score=1.0)]
    result = _rerank(items, entities)
    assert result[0]["score"] == pytest.approx(1.0 * 0.6 * 0.7, abs=1e-6)


def test_only_cuisine_penalty_when_protein_differs():
    entities = {
        "recent_cuisines": ["indian", "indian"],
        "recent_protein_sources": ["chicken"],
    }
    items = [_item("r1", title="Lamb Rogan Josh", cuisine="indian", score=1.0)]
    result = _rerank(items, entities)
    assert result[0]["score"] == pytest.approx(0.6, abs=1e-6)


def test_only_protein_penalty_when_cuisine_not_overused():
    entities = {
        "recent_cuisines": ["indian", "italian"],  # different → no cuisine penalty
        "recent_protein_sources": ["chicken"],
    }
    items = [_item("r1", title="Chicken Curry", cuisine="indian", score=1.0)]
    result = _rerank(items, entities)
    assert result[0]["score"] == pytest.approx(0.7, abs=1e-6)


# ── Backward compatibility ─────────────────────────────────────────────────────


def test_no_variety_signals_returns_fused_unchanged():
    items = [_item("r1", score=1.0), _item("r2", score=0.8)]
    result = contextual_rerank(items, {})
    assert result is items
