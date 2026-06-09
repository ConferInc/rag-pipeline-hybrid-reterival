"""
Tier 1 PR 2 — FDA allergen synonym dictionary tests.

Covers:
  _expand_fda_allergen_synonyms — synonym group lookup and fallback
  _filter_allergens end-to-end  — catches synonym ingredient names
  All 9 FDA allergen groups: milk, egg, peanut, tree nut, wheat, soy,
    fish, shellfish, sesame.

FDA source: FALCPA (2004) + FASTER Act (2021) — 21 CFR 117.
"""

from __future__ import annotations

import pytest

from rag_pipeline.orchestrator.constraint_filter import (
    _FDA_ALLERGEN_SYNONYMS,
    _expand_fda_allergen_synonyms,
    _filter_allergens,
)


# ── _expand_fda_allergen_synonyms ─────────────────────────────────────────────


def test_peanut_expands_to_groundnut():
    result = _expand_fda_allergen_synonyms("peanut")
    assert "groundnut" in result
    assert "peanut butter" in result
    assert "arachis oil" in result


def test_milk_expands_to_buttermilk_and_ghee():
    result = _expand_fda_allergen_synonyms("milk")
    assert "buttermilk" in result
    assert "ghee" in result
    assert "whey" in result
    assert "casein" in result


def test_sesame_expands_to_tahini():
    result = _expand_fda_allergen_synonyms("sesame")
    assert "tahini" in result
    assert "sesame oil" in result
    assert "gingelly" in result


def test_soy_expands_to_tofu_and_tamari():
    result = _expand_fda_allergen_synonyms("soy")
    assert "tofu" in result
    assert "tamari" in result
    assert "miso" in result
    assert "edamame" in result


def test_wheat_expands_to_spelt_and_semolina():
    result = _expand_fda_allergen_synonyms("wheat")
    assert "spelt" in result
    assert "semolina" in result
    assert "bulgur" in result
    assert "seitan" in result


def test_egg_expands_to_albumin_and_meringue():
    result = _expand_fda_allergen_synonyms("egg")
    assert "albumin" in result
    assert "meringue" in result
    assert "mayonnaise" in result


def test_fish_expands_to_specific_species():
    result = _expand_fda_allergen_synonyms("fish")
    assert "salmon" in result
    assert "anchovy" in result
    assert "fish sauce" in result


def test_shellfish_expands_to_shrimp_and_lobster():
    result = _expand_fda_allergen_synonyms("shellfish")
    assert "shrimp" in result
    assert "lobster" in result
    assert "crab" in result
    assert "prawn" in result


def test_tree_nut_expands_to_almond_and_cashew():
    result = _expand_fda_allergen_synonyms("tree nut")
    assert "almond" in result
    assert "cashew" in result
    assert "pistachio" in result
    assert "hazelnut" in result


# ── Reverse lookup: specific ingredient → full allergen group ─────────────────


def test_groundnut_reverse_expands_to_peanut_group():
    result = _expand_fda_allergen_synonyms("groundnut")
    assert "peanut" in result
    assert "peanut butter" in result


def test_tahini_reverse_expands_to_sesame_group():
    result = _expand_fda_allergen_synonyms("tahini")
    assert "sesame" in result
    assert "sesame oil" in result


def test_tofu_reverse_expands_to_soy_group():
    result = _expand_fda_allergen_synonyms("tofu")
    assert "soy" in result
    assert "edamame" in result


def test_almond_reverse_expands_to_tree_nut_group():
    result = _expand_fda_allergen_synonyms("almond")
    assert "cashew" in result
    assert "walnut" in result


def test_seitan_reverse_expands_to_wheat_group():
    result = _expand_fda_allergen_synonyms("seitan")
    assert "wheat" in result
    assert "spelt" in result


# ── Plural handling ───────────────────────────────────────────────────────────


def test_plural_peanuts_expands_to_peanut_group():
    result = _expand_fda_allergen_synonyms("peanuts")
    assert "groundnut" in result


def test_plural_eggs_expands_to_egg_group():
    result = _expand_fda_allergen_synonyms("eggs")
    assert "albumin" in result


# ── Unknown / non-allergen term falls back to itself ─────────────────────────


def test_unknown_term_returns_itself():
    result = _expand_fda_allergen_synonyms("paprika")
    assert result == {"paprika"}


def test_empty_term_returns_itself():
    result = _expand_fda_allergen_synonyms("  ")
    assert result == {""}


# ── FDA dictionary structural checks ─────────────────────────────────────────


def test_all_nine_fda_groups_present():
    keys = set(_FDA_ALLERGEN_SYNONYMS.keys())
    assert "milk" in keys
    assert "egg" in keys
    assert "peanut" in keys
    assert "tree nut" in keys
    assert "wheat" in keys
    assert "soy" in keys
    assert "fish" in keys
    assert "shellfish" in keys
    assert "sesame" in keys


def test_each_group_primary_name_is_in_its_own_set():
    """Each allergen key must appear in its own synonym set."""
    for key, synonyms in _FDA_ALLERGEN_SYNONYMS.items():
        assert key in synonyms, f"Primary key '{key}' not in its own synonym set"


# ── _filter_allergens end-to-end with synonym expansion ──────────────────────


def _make_recipe(rid: str | None, title: str = "Test") -> dict:
    if rid:
        return {"title": title, "payload": {"id": rid}}
    return {"title": title, "payload": {}}


def test_filter_catches_groundnut_when_user_has_peanut_allergy(monkeypatch):
    """Core PR 2 scenario: ingredient named 'groundnut' must be caught for peanut allergy."""
    monkeypatch.setattr(
        "rag_pipeline.orchestrator.constraint_filter._fetch_allergen_violating_ids",
        lambda driver, recipe_ids, allergens, db: {
            "r1" for _ in [None] if "groundnut" in allergens
        },
    )
    fused = [_make_recipe("r1", "Groundnut Stew"), _make_recipe("r2", "Garden Salad")]
    result = _filter_allergens(fused, ["peanut"], driver=object(), database=None)
    ids = [r["payload"]["id"] for r in result]
    assert "r1" not in ids
    assert "r2" in ids


def test_filter_catches_ghee_when_user_has_milk_allergy(monkeypatch):
    """'ghee' is a milk derivative; must be caught when user allergen is 'milk'."""
    monkeypatch.setattr(
        "rag_pipeline.orchestrator.constraint_filter._fetch_allergen_violating_ids",
        lambda driver, recipe_ids, allergens, db: {
            "r1" for _ in [None] if "ghee" in allergens
        },
    )
    fused = [_make_recipe("r1", "Ghee Rice"), _make_recipe("r2", "Steamed Veg")]
    result = _filter_allergens(fused, ["milk"], driver=object(), database=None)
    ids = [r["payload"]["id"] for r in result]
    assert "r1" not in ids
    assert "r2" in ids


def test_filter_catches_tahini_when_user_has_sesame_allergy(monkeypatch):
    monkeypatch.setattr(
        "rag_pipeline.orchestrator.constraint_filter._fetch_allergen_violating_ids",
        lambda driver, recipe_ids, allergens, db: {
            "r1" for _ in [None] if "tahini" in allergens
        },
    )
    fused = [_make_recipe("r1", "Hummus"), _make_recipe("r2", "Roast Chicken")]
    result = _filter_allergens(fused, ["sesame"], driver=object(), database=None)
    ids = [r["payload"]["id"] for r in result]
    assert "r1" not in ids
    assert "r2" in ids


def test_filter_catches_seitan_for_wheat_allergy(monkeypatch):
    monkeypatch.setattr(
        "rag_pipeline.orchestrator.constraint_filter._fetch_allergen_violating_ids",
        lambda driver, recipe_ids, allergens, db: {
            "r1" for _ in [None] if "seitan" in allergens
        },
    )
    fused = [_make_recipe("r1", "Seitan Stir Fry"), _make_recipe("r2", "Rice Bowl")]
    result = _filter_allergens(fused, ["wheat"], driver=object(), database=None)
    ids = [r["payload"]["id"] for r in result]
    assert "r1" not in ids
    assert "r2" in ids


def test_non_allergen_term_still_filters_on_exact_match(monkeypatch):
    """A term not in any FDA group still filters on its own name."""
    monkeypatch.setattr(
        "rag_pipeline.orchestrator.constraint_filter._fetch_allergen_violating_ids",
        lambda driver, recipe_ids, allergens, db: {
            "r1" for _ in [None] if "mango" in allergens
        },
    )
    fused = [_make_recipe("r1", "Mango Salsa"), _make_recipe("r2", "Berry Smoothie")]
    result = _filter_allergens(fused, ["mango"], driver=object(), database=None)
    ids = [r["payload"]["id"] for r in result]
    assert "r1" not in ids
    assert "r2" in ids
