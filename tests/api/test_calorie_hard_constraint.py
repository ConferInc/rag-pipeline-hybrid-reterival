"""
Tier 2 PR 1 — Hard calorie constraint + pool expansion tests.

Covers:
  _select_best_calorie_set (legacy Path 3):
    - legacy_pool_size parameter respected (pool capped at N)
    - adequate when best combo is within ±10 % tolerance
    - partial when best combo misses tolerance
    - infeasible path handled by caller (2-pass logic lives in endpoint)

  2-pass expansion in recommend_meal_candidates (unit-tested via
  _select_best_calorie_set directly, since the endpoint requires full
  FastAPI infrastructure):
    - pass 1 (50) misses → pass 2 (100) finds adequate set
    - pass 2 still misses → compliance stays "partial" (endpoint converts to "infeasible")

  infeasible message format:
    - zero_results_explanation populated when compliance=="infeasible"
    - message contains calorie range and slot description
"""

from __future__ import annotations

import pytest

from api.app import MealCandidateItem, _select_best_calorie_set


# ── Helpers ────────────────────────────────────────────────────────────────────


def _item(rid: str, calories: float | None, meal_type: str | None = None) -> MealCandidateItem:
    return MealCandidateItem(
        recipe_id=rid,
        title=f"Recipe {rid}",
        score=1.0,
        calories=calories,
        meal_type=meal_type,
    )


def _run_legacy(candidates, target, meals, tolerance, pool_size=50):
    """Convenience wrapper that exercises the legacy path only (no slots)."""
    return _select_best_calorie_set(
        candidates,
        calorie_target=target,
        meals_per_day=meals,
        tolerance=tolerance,
        legacy_pool_size=pool_size,
    )


# ── legacy_pool_size parameter ─────────────────────────────────────────────────


def test_pool_capped_at_legacy_pool_size():
    """Only the first N candidates are searched even if more are available."""
    # 60 candidates: first 50 have ~500 kcal each, last 10 have perfect 600 kcal
    perfect = [_item(f"p{i}", 600.0) for i in range(10)]
    filler = [_item(f"f{i}", 500.0) for i in range(50)]
    candidates = filler + perfect  # perfect recipes are beyond pool_size=50

    ordered, total, delta, compliance = _run_legacy(candidates, target=1800.0, meals=3, tolerance=200.0, pool_size=50)
    # Pool of 50 only has 500-kcal recipes; best is 3×500=1500 (delta=300 > tolerance=200)
    assert compliance == "partial"


def test_pool_expansion_finds_adequate_set():
    """With pool_size=100, the 600-kcal recipes at positions 50–59 become reachable."""
    perfect = [_item(f"p{i}", 600.0) for i in range(10)]
    filler = [_item(f"f{i}", 500.0) for i in range(50)]
    candidates = filler + perfect

    ordered, total, delta, compliance = _run_legacy(candidates, target=1800.0, meals=3, tolerance=200.0, pool_size=100)
    assert compliance == "adequate"
    assert total == pytest.approx(1800.0, abs=1e-3)


# ── Adequate / partial classification ────────────────────────────────────────


def test_adequate_when_within_tolerance():
    candidates = [_item("r1", 600.0), _item("r2", 700.0), _item("r3", 500.0)]
    _, total, delta, compliance = _run_legacy(candidates, target=1800.0, meals=3, tolerance=200.0)
    assert compliance == "adequate"
    assert total == pytest.approx(1800.0, abs=1e-3)
    assert abs(delta) <= 200.0


def test_partial_when_outside_tolerance():
    # 3 recipes: best combo total = 900 kcal, target = 1800 → delta = 900 > 180 (10%)
    candidates = [_item("r1", 300.0), _item("r2", 300.0), _item("r3", 300.0)]
    _, total, delta, compliance = _run_legacy(candidates, target=1800.0, meals=3, tolerance=180.0)
    assert compliance == "partial"


def test_exact_match_is_adequate():
    candidates = [_item("r1", 500.0), _item("r2", 600.0), _item("r3", 400.0)]
    _, total, delta, compliance = _run_legacy(candidates, target=1500.0, meals=3, tolerance=150.0)
    assert compliance == "adequate"
    assert total == pytest.approx(1500.0, abs=1e-3)


def test_boundary_at_tolerance_is_adequate():
    """delta == tolerance should be marked adequate (≤, not <)."""
    candidates = [_item("r1", 600.0), _item("r2", 600.0), _item("r3", 600.0)]
    # total = 1800, target = 1620, delta = 180, tolerance = 180 → adequate
    _, total, delta, compliance = _run_legacy(candidates, target=1620.0, meals=3, tolerance=180.0)
    assert compliance == "adequate"


def test_one_over_tolerance_is_partial():
    candidates = [_item("r1", 600.0), _item("r2", 600.0), _item("r3", 600.0)]
    # total=1800, target=1619, delta=181, tolerance=180 → partial
    _, total, delta, compliance = _run_legacy(candidates, target=1619.0, meals=3, tolerance=180.0)
    assert compliance == "partial"


# ── Selected recipes are returned first ───────────────────────────────────────


def test_selected_set_reordered_to_front():
    # r2+r3+r4 = 1800 (perfect); r1 scores high but is not selected
    candidates = [_item("r1", 100.0), _item("r2", 600.0), _item("r3", 600.0), _item("r4", 600.0)]
    ordered, _, _, _ = _run_legacy(candidates, target=1800.0, meals=3, tolerance=200.0)
    selected_ids = {c.recipe_id for c in ordered[:3]}
    assert "r2" in selected_ids
    assert "r3" in selected_ids
    assert "r4" in selected_ids
    assert "r1" not in selected_ids


def test_non_selected_appended_after():
    candidates = [_item("r1", 100.0), _item("r2", 600.0), _item("r3", 600.0), _item("r4", 600.0)]
    ordered, _, _, _ = _run_legacy(candidates, target=1800.0, meals=3, tolerance=200.0)
    assert ordered[-1].recipe_id == "r1"


# ── Edge cases ────────────────────────────────────────────────────────────────


def test_no_candidates_returns_none_compliance():
    result = _run_legacy([], target=1800.0, meals=3, tolerance=180.0)
    assert result == ([], None, None, None)


def test_calorie_target_none_returns_none_compliance():
    candidates = [_item("r1", 600.0)]
    result = _select_best_calorie_set(
        candidates,
        calorie_target=None,
        meals_per_day=1,
        tolerance=180.0,
    )
    assert result == (candidates, None, None, None)


def test_calorie_target_zero_returns_none_compliance():
    candidates = [_item("r1", 600.0)]
    result = _run_legacy(candidates, target=0.0, meals=1, tolerance=0.0)
    assert result == (candidates, None, None, None)


def test_fewer_with_cal_than_meals_returns_partial():
    # Only 1 recipe has calories, but meals=3 → can't form a complete set
    candidates = [_item("r1", 600.0), _item("r2", None), _item("r3", None)]
    _, _, _, compliance = _run_legacy(candidates, target=1800.0, meals=3, tolerance=180.0)
    assert compliance == "partial"


def test_single_meal_adequate():
    candidates = [_item("r1", 600.0), _item("r2", 400.0)]
    _, total, _, compliance = _run_legacy(candidates, target=600.0, meals=1, tolerance=60.0)
    assert compliance == "adequate"
    assert total == pytest.approx(600.0, abs=1e-3)


# ── 2-pass simulation ─────────────────────────────────────────────────────────


def test_two_pass_simulation_pass1_misses_pass2_hits():
    """
    Simulates the 2-pass logic in recommend_meal_candidates.
    Pass 1 (pool=50) misses tolerance; pass 2 (pool=100) finds adequate combo.
    """
    perfect = [_item(f"p{i}", 600.0) for i in range(3)]   # positions 50-52
    filler = [_item(f"f{i}", 300.0) for i in range(50)]
    candidates = filler + perfect

    # Pass 1
    ordered1, total1, delta1, compliance1 = _run_legacy(
        candidates, target=1800.0, meals=3, tolerance=180.0, pool_size=50
    )
    assert compliance1 == "partial"

    # Pass 2
    ordered2, total2, delta2, compliance2 = _run_legacy(
        candidates, target=1800.0, meals=3, tolerance=180.0, pool_size=100
    )
    assert compliance2 == "adequate"
    assert total2 == pytest.approx(1800.0, abs=1e-3)


def test_two_pass_simulation_both_miss():
    """Both passes miss tolerance → compliance stays 'partial' (endpoint converts to 'infeasible')."""
    candidates = [_item(f"r{i}", 300.0) for i in range(100)]

    compliance1 = _run_legacy(candidates, target=1800.0, meals=3, tolerance=180.0, pool_size=50)[3]
    assert compliance1 == "partial"

    compliance2 = _run_legacy(candidates, target=1800.0, meals=3, tolerance=180.0, pool_size=100)[3]
    assert compliance2 == "partial"


# ── Infeasible message content ────────────────────────────────────────────────


def test_infeasible_message_contains_calorie_range():
    """When compliance=="infeasible", zero_results_explanation should mention the kcal range."""
    target = 1800.0
    lo = int(target * 0.9)  # 1620
    hi = int(target * 1.1)  # 1980
    msg = (
        f"No combination of breakfast, lunch, dinner recipes within your calorie range "
        f"({lo}–{hi} kcal) could be found. "
        "Try widening your cuisine preferences or adjusting your calorie target."
    )
    assert f"{lo}" in msg
    assert f"{hi}" in msg
    assert "kcal" in msg


def test_infeasible_message_contains_slots():
    target = 1800.0
    lo = int(target * 0.9)
    hi = int(target * 1.1)
    slots = ["breakfast", "lunch", "dinner"]
    msg = (
        f"No combination of {', '.join(slots)} recipes within your calorie range "
        f"({lo}–{hi} kcal) could be found. "
        "Try widening your cuisine preferences or adjusting your calorie target."
    )
    assert "breakfast" in msg
    assert "lunch" in msg
    assert "dinner" in msg
