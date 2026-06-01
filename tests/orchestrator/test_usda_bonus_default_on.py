"""
Tier 2 PR 2 — USDA food-group bonus default-on tests.

Confirms that ENABLE_USDA_FOOD_GROUP_BONUS now defaults to "1" (on), so the
diversity multiplier fires without explicit env-var configuration. Also verifies
the emergency-disable path (setting the var to "0") still works.
"""

from __future__ import annotations

import os

import pytest

from rag_pipeline.orchestrator.constraint_filter import apply_usda_food_group_bonus


def _make_item(rid: str, food_groups: list[str], score: float = 1.0) -> dict:
    return {
        "payload": {"id": rid, "food_groups": food_groups},
        "rrf_score": score,
        "score": score,
        "sources": [],
    }


_ENTITIES_WITH_USDA = {"usda_guidelines": {"groups": ["vegetables", "protein"]}}
_INTENT = "find_recipe"


# ── Default on ────────────────────────────────────────────────────────────────


def test_bonus_fires_by_default_without_env_var(monkeypatch):
    """With no env var set, bonus should apply (default is now '1')."""
    monkeypatch.delenv("ENABLE_USDA_FOOD_GROUP_BONUS", raising=False)

    high = _make_item("r_high", ["protein", "dairy", "vegetables", "fruits", "whole_grains"], score=1.0)
    low = _make_item("r_low", [], score=1.0)

    result = apply_usda_food_group_bonus([high, low], _ENTITIES_WITH_USDA, _INTENT)
    ids = [r["payload"]["id"] for r in result]
    # r_high covers all 5 groups → multiplier > 1.0 → sorted first
    assert ids[0] == "r_high"


def test_bonus_fires_when_env_var_explicitly_one(monkeypatch):
    monkeypatch.setenv("ENABLE_USDA_FOOD_GROUP_BONUS", "1")

    high = _make_item("r_high", ["protein", "dairy", "vegetables", "fruits", "whole_grains"], score=1.0)
    low = _make_item("r_low", [], score=1.0)

    result = apply_usda_food_group_bonus([high, low], _ENTITIES_WITH_USDA, _INTENT)
    ids = [r["payload"]["id"] for r in result]
    assert ids[0] == "r_high"


# ── Emergency disable ─────────────────────────────────────────────────────────


def test_bonus_disabled_when_env_var_zero(monkeypatch):
    """Setting ENABLE_USDA_FOOD_GROUP_BONUS=0 must disable the bonus."""
    monkeypatch.setenv("ENABLE_USDA_FOOD_GROUP_BONUS", "0")

    items = [
        _make_item("r1", ["protein", "dairy", "vegetables", "fruits", "whole_grains"], score=2.0),
        _make_item("r2", [], score=1.0),
    ]
    result = apply_usda_food_group_bonus(items, _ENTITIES_WITH_USDA, _INTENT)
    # When disabled, function returns fused unchanged — same order, same score objects
    assert result[0]["payload"]["id"] == "r1"
    assert result[0]["score"] == 2.0  # score not modified


def test_bonus_disabled_when_env_var_empty_string(monkeypatch):
    """Explicit empty string must disable the bonus (opt-out path)."""
    monkeypatch.setenv("ENABLE_USDA_FOOD_GROUP_BONUS", "")

    items = [_make_item("r1", ["protein"], score=1.0)]
    original_score = items[0]["score"]
    result = apply_usda_food_group_bonus(items, _ENTITIES_WITH_USDA, _INTENT)
    # Returned unchanged
    assert result is items


# ── Fail-safe: missing usda_guidelines returns fused unchanged ─────────────────


def test_missing_usda_guidelines_returns_fused_unchanged(monkeypatch):
    monkeypatch.delenv("ENABLE_USDA_FOOD_GROUP_BONUS", raising=False)

    items = [_make_item("r1", ["protein"], score=1.0)]
    result = apply_usda_food_group_bonus(items, {}, _INTENT)
    assert result is items


def test_non_recipe_intent_returns_fused_unchanged(monkeypatch):
    monkeypatch.delenv("ENABLE_USDA_FOOD_GROUP_BONUS", raising=False)

    items = [_make_item("r1", ["protein"], score=1.0)]
    result = apply_usda_food_group_bonus(items, _ENTITIES_WITH_USDA, "get_nutrition_info")
    assert result is items


# ── Score adjustment sanity check ─────────────────────────────────────────────


def test_all_five_groups_gets_max_multiplier(monkeypatch):
    monkeypatch.delenv("ENABLE_USDA_FOOD_GROUP_BONUS", raising=False)

    all_groups = ["protein", "dairy", "vegetables", "fruits", "whole_grains"]
    item = _make_item("r1", all_groups, score=1.0)
    result = apply_usda_food_group_bonus([item], _ENTITIES_WITH_USDA, _INTENT)
    # max_mult is 1.2, coverage=1.0 → score = 1.0 * 1.2 = 1.2
    assert result[0]["score"] == pytest.approx(1.2, abs=1e-6)


def test_no_groups_score_unchanged(monkeypatch):
    monkeypatch.delenv("ENABLE_USDA_FOOD_GROUP_BONUS", raising=False)

    item = _make_item("r1", [], score=1.0)
    result = apply_usda_food_group_bonus([item], _ENTITIES_WITH_USDA, _INTENT)
    # food_group_balance_score returns 1.0 when no groups → score unchanged
    assert result[0]["score"] == pytest.approx(1.0, abs=1e-6)


def test_bonus_sorts_higher_coverage_first(monkeypatch):
    monkeypatch.delenv("ENABLE_USDA_FOOD_GROUP_BONUS", raising=False)

    items = [
        _make_item("r_poor", ["protein"], score=1.0),
        _make_item("r_rich", ["protein", "dairy", "vegetables", "fruits", "whole_grains"], score=1.0),
    ]
    result = apply_usda_food_group_bonus(items, _ENTITIES_WITH_USDA, _INTENT)
    assert result[0]["payload"]["id"] == "r_rich"
