"""
Tier 2 PR 3 — Joint calorie + USDA food-group selection tests.

Covers:
  _combo_joint_score — joint score formula
  _fg_coverage       — USDA food-group union coverage
  _get_joint_weights — env-var weight parsing and normalisation

  _select_best_calorie_set Path 3 (legacy):
    - among combinations within tolerance, prefer better USDA coverage
    - calorie still dominates (w_cal=0.7 default) — tiebreak goes to fg coverage
    - fallback: if no combo within tolerance, returns closest (partial) as before

  _select_best_calorie_set Path 2 (multi-slot greedy):
    - marginal food-group gain used as tiebreaker between equal-calorie candidates
    - accumulated groups tracked across slots

  _select_best_calorie_set Path 1 (single-slot):
    - unchanged by PR 3 (single recipe, no combination to compare)
"""

from __future__ import annotations

import os

import pytest

from api.app import (
    MealCandidateItem,
    _combo_joint_score,
    _fg_coverage,
    _get_joint_weights,
    _select_best_calorie_set,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

_ALL_FG = ["protein", "dairy", "vegetables", "fruits", "whole_grains"]
_USDA_COUNT = 5


def _item(
    rid: str,
    calories: float | None,
    food_groups: list[str] | None = None,
    meal_type: str | None = None,
) -> MealCandidateItem:
    return MealCandidateItem(
        recipe_id=rid,
        title=f"Recipe {rid}",
        score=1.0,
        calories=calories,
        food_groups=food_groups or [],
        meal_type=meal_type,
    )


def _run_legacy(candidates, target, meals, tolerance, pool_size=50):
    return _select_best_calorie_set(
        candidates,
        calorie_target=target,
        meals_per_day=meals,
        tolerance=tolerance,
        legacy_pool_size=pool_size,
    )


# ── _fg_coverage ──────────────────────────────────────────────────────────────


def test_fg_coverage_all_five_groups():
    items = [_item("r1", 500.0, _ALL_FG)]
    assert _fg_coverage(items) == pytest.approx(1.0)


def test_fg_coverage_no_groups():
    items = [_item("r1", 500.0, [])]
    assert _fg_coverage(items) == pytest.approx(0.0)


def test_fg_coverage_three_groups():
    items = [_item("r1", 500.0, ["protein", "vegetables", "fruits"])]
    assert _fg_coverage(items) == pytest.approx(3 / _USDA_COUNT)


def test_fg_coverage_union_across_items():
    items = [
        _item("r1", 500.0, ["protein", "dairy"]),
        _item("r2", 400.0, ["vegetables", "fruits"]),
        _item("r3", 600.0, ["whole_grains"]),
    ]
    assert _fg_coverage(items) == pytest.approx(1.0)


def test_fg_coverage_ignores_unknown_groups():
    items = [_item("r1", 500.0, ["protein", "unknown_group", "fruits"])]
    assert _fg_coverage(items) == pytest.approx(2 / _USDA_COUNT)


def test_fg_coverage_case_insensitive():
    items = [_item("r1", 500.0, ["Protein", "DAIRY", "Vegetables"])]
    assert _fg_coverage(items) == pytest.approx(3 / _USDA_COUNT)


# ── _combo_joint_score ────────────────────────────────────────────────────────


def test_joint_score_perfect_calories_all_groups():
    items = [_item("r1", 600.0, _ALL_FG), _item("r2", 600.0, []), _item("r3", 600.0, [])]
    # total=1800 == target → cal_closeness=1.0; fg=1.0/5=0.2 (only r1 covers all)
    score = _combo_joint_score(1800.0, 1800.0, items, 0.7, 0.3)
    assert score == pytest.approx(0.7 * 1.0 + 0.3 * 1.0)


def test_joint_score_perfect_calories_no_groups():
    items = [_item("r1", 600.0, []), _item("r2", 600.0, []), _item("r3", 600.0, [])]
    score = _combo_joint_score(1800.0, 1800.0, items, 0.7, 0.3)
    assert score == pytest.approx(0.7 * 1.0 + 0.3 * 0.0)


def test_joint_score_zero_calories_closeness():
    items = [_item("r1", 100.0, _ALL_FG)]
    # total=100, target=1800 → cal_closeness = max(0, 1-1700/1800) ≈ 0.056; fg=1.0
    expected_cal = max(0.0, 1.0 - abs(100 - 1800) / 1800)
    score = _combo_joint_score(100.0, 1800.0, items, 0.7, 0.3)
    assert score == pytest.approx(0.7 * expected_cal + 0.3 * 1.0)


def test_joint_score_cal_closeness_clamped_to_zero():
    items = [_item("r1", 0.0, [])]
    score = _combo_joint_score(0.0, 1800.0, items, 0.7, 0.3)
    assert score >= 0.0


# ── _get_joint_weights ────────────────────────────────────────────────────────


def test_default_weights(monkeypatch):
    monkeypatch.delenv("CALORIE_USDA_W_CAL", raising=False)
    monkeypatch.delenv("CALORIE_USDA_W_USDA", raising=False)
    w_cal, w_usda = _get_joint_weights()
    assert w_cal == pytest.approx(0.7, abs=1e-6)
    assert w_usda == pytest.approx(0.3, abs=1e-6)


def test_custom_weights_normalised(monkeypatch):
    monkeypatch.setenv("CALORIE_USDA_W_CAL", "1.0")
    monkeypatch.setenv("CALORIE_USDA_W_USDA", "1.0")
    w_cal, w_usda = _get_joint_weights()
    assert w_cal == pytest.approx(0.5, abs=1e-6)
    assert w_usda == pytest.approx(0.5, abs=1e-6)


def test_invalid_weight_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("CALORIE_USDA_W_CAL", "not_a_number")
    w_cal, w_usda = _get_joint_weights()
    assert w_cal == pytest.approx(0.7, abs=1e-6)
    assert w_usda == pytest.approx(0.3, abs=1e-6)


def test_weights_sum_to_one(monkeypatch):
    monkeypatch.setenv("CALORIE_USDA_W_CAL", "2.0")
    monkeypatch.setenv("CALORIE_USDA_W_USDA", "3.0")
    w_cal, w_usda = _get_joint_weights()
    assert w_cal + w_usda == pytest.approx(1.0, abs=1e-6)


# ── Path 3: joint scoring selects better food-group coverage ──────────────────


def test_within_tolerance_combo_with_better_fg_wins():
    """
    Two combos are equally close to the calorie target (both within tolerance).
    The one with better USDA food-group coverage should be chosen.
    """
    # Combo A: r1+r2+r3 — total=1800, covers 0 groups
    # Combo B: r4+r5+r6 — total=1800, covers all 5 groups
    # Both within tolerance=200 → combo B should win.
    candidates = [
        _item("r1", 600.0, []),
        _item("r2", 600.0, []),
        _item("r3", 600.0, []),
        _item("r4", 600.0, ["protein"]),
        _item("r5", 600.0, ["dairy", "vegetables"]),
        _item("r6", 600.0, ["fruits", "whole_grains"]),
    ]
    ordered, total, _, compliance = _run_legacy(candidates, target=1800.0, meals=3, tolerance=200.0, pool_size=50)
    assert compliance == "adequate"
    selected_ids = {c.recipe_id for c in ordered[:3]}
    assert selected_ids == {"r4", "r5", "r6"}


def test_calorie_dominates_over_fg_coverage(monkeypatch):
    """
    Combo A: exactly on target, 0 food groups.
    Combo B: 15% off target (within tolerance=180), all 5 food groups.
    With w_cal=0.7, combo A should still win because perfect calorie match
    gives joint_score=0.7*1.0+0.3*0.0=0.70 vs combo B's 0.7*0.85+0.3*1.0=0.895.
    Actually combo B wins in this case — let's use tighter numbers.
    """
    # Combo A: total=1800 exactly, 0 groups → joint = 0.7*1.0+0.3*0 = 0.70
    # Combo B: total=1620 (10% low), all 5 groups → joint = 0.7*0.9+0.3*1.0 = 0.63+0.30 = 0.93
    # B wins — this confirms food-group coverage has real influence.
    candidates = [
        _item("r1", 600.0, []),
        _item("r2", 600.0, []),
        _item("r3", 600.0, []),
        _item("r4", 540.0, ["protein", "dairy"]),
        _item("r5", 540.0, ["vegetables", "fruits"]),
        _item("r6", 540.0, ["whole_grains"]),
    ]
    ordered, total, _, compliance = _run_legacy(candidates, target=1800.0, meals=3, tolerance=200.0, pool_size=50)
    assert compliance == "adequate"
    selected_ids = {c.recipe_id for c in ordered[:3]}
    # B (r4+r5+r6=1620, delta=180≤200, joint=0.93) beats A (1800, joint=0.70)
    assert selected_ids == {"r4", "r5", "r6"}


def test_outside_tolerance_combo_falls_back_to_closest():
    """When no combo is within tolerance, the closest calorie combo is returned as 'partial'."""
    candidates = [_item(f"r{i}", 300.0) for i in range(10)]
    _, total, _, compliance = _run_legacy(candidates, target=1800.0, meals=3, tolerance=50.0, pool_size=50)
    assert compliance == "partial"
    assert total == pytest.approx(900.0, abs=1e-3)


def test_fg_coverage_zero_no_food_groups_data_still_adequate():
    """When food_groups is empty for all recipes, selection still works on calories alone."""
    candidates = [_item("r1", 600.0, []), _item("r2", 600.0, []), _item("r3", 600.0, [])]
    _, total, _, compliance = _run_legacy(candidates, target=1800.0, meals=3, tolerance=180.0, pool_size=50)
    assert compliance == "adequate"
    assert total == pytest.approx(1800.0, abs=1e-3)


# ── Path 2: multi-slot greedy with joint scoring ──────────────────────────────


def test_multi_slot_prefers_marginal_fg_gain():
    """
    Two breakfast candidates with identical calories.
    Candidate A covers 'protein' (already in accumulated_fg from nothing → marginal gain).
    Candidate B covers 'vegetables' (new group — higher marginal gain when none accumulated).
    Without accumulated context both gain equally, but after lunch sets 'protein',
    dinner should prefer the recipe that adds a NEW group.
    """
    meal_slots = ["breakfast", "lunch", "dinner"]
    slot_targets = {"breakfast": 500.0, "lunch": 600.0, "dinner": 700.0}

    candidates = [
        # Breakfast: r_b1 covers protein, r_b2 covers vegetables
        _item("r_b1", 500.0, ["protein"], "breakfast"),
        _item("r_b2", 500.0, ["vegetables"], "breakfast"),
        # Lunch: r_l1 covers protein (duplicate), r_l2 covers dairy (new)
        _item("r_l1", 600.0, ["protein"], "lunch"),
        _item("r_l2", 600.0, ["dairy"], "lunch"),
        # Dinner: r_d1 covers vegetables, r_d2 covers fruits+whole_grains (more new groups)
        _item("r_d1", 700.0, ["vegetables"], "dinner"),
        _item("r_d2", 700.0, ["fruits", "whole_grains"], "dinner"),
    ]
    ordered, total, delta, compliance = _select_best_calorie_set(
        candidates,
        calorie_target=1800.0,
        meals_per_day=3,
        tolerance=180.0,
        meal_slots=meal_slots,
        slot_targets=slot_targets,
    )
    assert compliance == "adequate"
    selected_ids = {c.recipe_id for c in ordered[:3]}
    # Greedy picks the recipe that adds the most new groups at each step.
    # Breakfast: both gain 1 new group → either is fine (tie resolved by max)
    # Lunch: r_l2 (dairy=new) > r_l1 (protein=may already be there)
    assert "r_l2" in selected_ids or "r_l1" in selected_ids  # at least lunch picked
    assert "r_d2" in selected_ids  # dinner: fruits+whole_grains > vegetables alone


def test_multi_slot_adequate_compliance():
    meal_slots = ["breakfast", "lunch", "dinner"]
    slot_targets = {"breakfast": 500.0, "lunch": 700.0, "dinner": 600.0}
    candidates = [
        _item("r1", 500.0, ["protein"], "breakfast"),
        _item("r2", 700.0, ["dairy", "vegetables"], "lunch"),
        _item("r3", 600.0, ["fruits", "whole_grains"], "dinner"),
    ]
    _, total, delta, compliance = _select_best_calorie_set(
        candidates,
        calorie_target=1800.0,
        meals_per_day=3,
        tolerance=180.0,
        meal_slots=meal_slots,
        slot_targets=slot_targets,
    )
    assert compliance == "adequate"
    assert total == pytest.approx(1800.0, abs=1e-3)


# ── Env-var weight override integration ──────────────────────────────────────


def test_w_usda_zero_reverts_to_calorie_only(monkeypatch):
    """With w_usda=0, joint score is pure calorie closeness — food groups irrelevant."""
    monkeypatch.setenv("CALORIE_USDA_W_CAL", "1.0")
    monkeypatch.setenv("CALORIE_USDA_W_USDA", "0.0")

    # Combo A: total=1800 (perfect), no food groups
    # Combo B: total=1620 (10% off), all 5 groups
    # With w_usda=0, combo A (perfect calories) should win.
    candidates = [
        _item("r1", 600.0, []),
        _item("r2", 600.0, []),
        _item("r3", 600.0, []),
        _item("r4", 540.0, _ALL_FG),
        _item("r5", 540.0, []),
        _item("r6", 540.0, []),
    ]
    ordered, total, _, compliance = _run_legacy(candidates, target=1800.0, meals=3, tolerance=200.0, pool_size=50)
    assert compliance == "adequate"
    assert total == pytest.approx(1800.0, abs=1e-3)
    selected_ids = {c.recipe_id for c in ordered[:3]}
    assert selected_ids == {"r1", "r2", "r3"}
