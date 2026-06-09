"""
Tests for Fix 3 — slot-aware calorie planning.

Covers:
  _compute_slot_targets  — goal-based percentage distribution
  _apply_calorie_fit_rerank — per-slot target scoring
  _select_best_calorie_set  — single-slot, multi-slot, and legacy paths
  MealCandidateItem.meal_type field
"""

from __future__ import annotations

import pytest

from api.app import (
    MealCandidateItem,
    _apply_calorie_fit_rerank,
    _compute_slot_targets,
    _select_best_calorie_set,
)


# ── _compute_slot_targets ──────────────────────────────────────────────────


def test_slot_targets_weight_loss_full_day():
    targets = _compute_slot_targets(2000.0, ["breakfast", "lunch", "dinner", "snack"], "weight_loss")
    assert abs(targets["breakfast"] - 400.0) < 1
    assert abs(targets["lunch"] - 700.0) < 1
    assert abs(targets["dinner"] - 600.0) < 1
    assert abs(targets["snack"] - 300.0) < 1
    assert abs(sum(targets.values()) - 2000.0) < 1


def test_slot_targets_muscle_gain_full_day():
    targets = _compute_slot_targets(2400.0, ["breakfast", "lunch", "dinner", "snack"], "muscle_gain")
    assert abs(targets["breakfast"] - 600.0) < 1
    assert abs(targets["lunch"] - 720.0) < 1
    assert abs(targets["dinner"] - 840.0) < 1
    assert abs(targets["snack"] - 240.0) < 1


def test_slot_targets_maintenance_three_meals():
    targets = _compute_slot_targets(2000.0, ["breakfast", "lunch", "dinner"], "maintenance")
    total = sum(targets.values())
    assert abs(total - 2000.0) < 1
    # breakfast(0.22) + lunch(0.33) + dinner(0.35) = 0.90, renormalised
    assert targets["dinner"] > targets["breakfast"]
    assert targets["lunch"] > targets["breakfast"]


def test_slot_targets_renormalises_partial_slots():
    targets = _compute_slot_targets(1500.0, ["breakfast", "lunch"], "weight_loss")
    total = sum(targets.values())
    assert abs(total - 1500.0) < 1


def test_slot_targets_unknown_goal_falls_back_to_maintenance():
    targets_unknown = _compute_slot_targets(2000.0, ["breakfast", "lunch", "dinner"], "keto_challenge")
    targets_maint = _compute_slot_targets(2000.0, ["breakfast", "lunch", "dinner"], "maintenance")
    assert targets_unknown == targets_maint


def test_slot_targets_none_goal_uses_maintenance():
    targets_none = _compute_slot_targets(2000.0, ["breakfast", "lunch", "dinner"], None)
    targets_maint = _compute_slot_targets(2000.0, ["breakfast", "lunch", "dinner"], "maintenance")
    assert targets_none == targets_maint


# ── _apply_calorie_fit_rerank (slot-aware) ────────────────────────────────


def _make_item(title: str, calories: float, meal_type: str = "", score: float = 1.0) -> dict:
    return {
        "rrf_score": score,
        "score": score,
        "payload": {"title": title, "calories": calories, "meal_type": meal_type},
    }


def test_rerank_single_slot_uses_slot_target():
    items = [
        _make_item("big breakfast", 800.0, "breakfast"),  # far from 400
        _make_item("light breakfast", 390.0, "breakfast"),  # near 400
    ]
    slot_targets = {"breakfast": 400.0}
    result = _apply_calorie_fit_rerank(
        items,
        calorie_target=2000.0,
        meals_per_day=3,
        slot_targets=slot_targets,
        request_meal_type="breakfast",
    )
    assert result[0]["payload"]["title"] == "light breakfast"


def test_rerank_per_item_slot_lookup():
    items = [
        _make_item("big dinner", 900.0, "dinner"),   # dinner target ~700 → far
        _make_item("light dinner", 680.0, "dinner"),  # near 700
        _make_item("big breakfast", 600.0, "breakfast"),  # breakfast target ~440 → far
        _make_item("light breakfast", 430.0, "breakfast"),  # near 440
    ]
    slot_targets = {"breakfast": 440.0, "dinner": 700.0}
    result = _apply_calorie_fit_rerank(
        items,
        calorie_target=2000.0,
        meals_per_day=3,
        slot_targets=slot_targets,
    )
    titles = [r["payload"]["title"] for r in result]
    assert titles.index("light breakfast") < titles.index("big breakfast")
    assert titles.index("light dinner") < titles.index("big dinner")


def test_rerank_falls_back_to_uniform_without_slot_targets():
    items = [
        _make_item("a", 1000.0),
        _make_item("b", 500.0),  # closer to 2000/3 ≈ 667
    ]
    result = _apply_calorie_fit_rerank(items, calorie_target=2000.0, meals_per_day=3)
    assert result[0]["payload"]["title"] == "b"


# ── _select_best_calorie_set ───────────────────────────────────────────────


def _make_candidate(recipe_id: str, calories: float | None, meal_type: str = "") -> MealCandidateItem:
    return MealCandidateItem(
        recipe_id=recipe_id,
        title=recipe_id,
        score=1.0,
        calories=calories,
        meal_type=meal_type,
    )


def test_select_single_slot_path_picks_closest_to_slot_target():
    candidates = [
        _make_candidate("r1", 550.0, "breakfast"),  # far from 440
        _make_candidate("r2", 420.0, "breakfast"),  # close to 440
        _make_candidate("r3", 400.0, "breakfast"),  # also close
    ]
    slot_targets = {"breakfast": 440.0}
    ordered, total, delta, compliance = _select_best_calorie_set(
        candidates,
        calorie_target=2000.0,
        meals_per_day=3,
        tolerance=200.0,
        slot_targets=slot_targets,
        request_meal_type="breakfast",
    )
    assert ordered[0].recipe_id == "r2"
    assert abs(total - 420.0) < 1


def test_select_single_slot_path_compliance():
    candidates = [_make_candidate("r1", 430.0, "breakfast")]
    slot_targets = {"breakfast": 440.0}
    _, _, delta, compliance = _select_best_calorie_set(
        candidates,
        calorie_target=2000.0,
        meals_per_day=3,
        tolerance=50.0,
        slot_targets=slot_targets,
        request_meal_type="breakfast",
    )
    assert compliance == "adequate"
    assert abs(delta) < 50


def test_select_single_slot_no_calories_returns_partial():
    candidates = [_make_candidate("r1", None, "breakfast")]
    slot_targets = {"breakfast": 440.0}
    _, total, _, compliance = _select_best_calorie_set(
        candidates,
        calorie_target=2000.0,
        meals_per_day=3,
        tolerance=100.0,
        slot_targets=slot_targets,
        request_meal_type="breakfast",
    )
    assert total is None
    assert compliance == "partial"


def test_select_multi_slot_picks_one_per_slot():
    candidates = [
        _make_candidate("b1", 420.0, "breakfast"),
        _make_candidate("b2", 350.0, "breakfast"),
        _make_candidate("l1", 700.0, "lunch"),
        _make_candidate("d1", 650.0, "dinner"),
    ]
    slot_targets = {"breakfast": 440.0, "lunch": 660.0, "dinner": 700.0}
    ordered, total, delta, compliance = _select_best_calorie_set(
        candidates,
        calorie_target=1800.0,
        meals_per_day=3,
        tolerance=200.0,
        meal_slots=["breakfast", "lunch", "dinner"],
        slot_targets=slot_targets,
    )
    selected = ordered[:3]
    meal_types = {c.meal_type for c in selected}
    assert meal_types == {"breakfast", "lunch", "dinner"}
    # best breakfast pick should be b1 (420 closer to 440 than 350)
    assert any(c.recipe_id == "b1" for c in selected)


def test_select_multi_slot_falls_back_to_legacy_when_slot_missing():
    candidates = [
        _make_candidate("b1", 420.0, "breakfast"),
        _make_candidate("l1", 700.0, "lunch"),
        # no dinner candidates
    ]
    slot_targets = {"breakfast": 440.0, "lunch": 660.0, "dinner": 700.0}
    ordered, total, delta, compliance = _select_best_calorie_set(
        candidates,
        calorie_target=1800.0,
        meals_per_day=2,
        tolerance=200.0,
        meal_slots=["breakfast", "lunch", "dinner"],
        slot_targets=slot_targets,
    )
    # Falls through to legacy path — should still return candidates
    assert len(ordered) == 2


def test_select_legacy_path_used_without_slot_info():
    candidates = [
        _make_candidate("r1", 600.0),
        _make_candidate("r2", 700.0),
        _make_candidate("r3", 650.0),
    ]
    ordered, total, _, compliance = _select_best_calorie_set(
        candidates,
        calorie_target=2000.0,
        meals_per_day=3,
        tolerance=100.0,
    )
    assert total is not None
    assert len(ordered) == 3


def test_meal_candidate_item_has_meal_type_field():
    item = MealCandidateItem(
        recipe_id="r1",
        title="Eggs",
        score=0.9,
        calories=350.0,
        meal_type="breakfast",
    )
    assert item.meal_type == "breakfast"


def test_meal_candidate_item_meal_type_defaults_none():
    item = MealCandidateItem(recipe_id="r1", title="Soup", score=0.8)
    assert item.meal_type is None
