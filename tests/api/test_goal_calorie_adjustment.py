"""
Tests for Section 5 Fix 2 — goal-adjusted calorie target.

Covers:
  _apply_goal_calorie_adjustment — deficit/surplus, floor, population gating
  log payload shape used for observability

Sources for the constants under test are documented at the constant
definitions in api/app.py (NIH/NHLBI, ISSN, DGA 2025-2030, AHA).
"""

from __future__ import annotations

from datetime import date

from api.app import (
    _GOAL_CALORIE_ADJUSTMENT,
    _GOAL_CALORIE_FLOOR,
    _apply_goal_calorie_adjustment,
    _derive_age_years,
)


# ── per-goal adjustment ────────────────────────────────────────────────────


def test_weight_loss_subtracts_500():
    adjusted, log = _apply_goal_calorie_adjustment(2200.0, "weight_loss", None)
    assert adjusted == 1700.0
    assert log["adjustment"] == -500.0
    assert log["floor_applied"] is False
    assert log["skipped_population"] is None


def test_muscle_gain_adds_300():
    adjusted, log = _apply_goal_calorie_adjustment(2200.0, "muscle_gain", None)
    assert adjusted == 2500.0
    assert log["adjustment"] == 300.0


def test_maintenance_no_change():
    adjusted, log = _apply_goal_calorie_adjustment(2200.0, "maintenance", None)
    assert adjusted == 2200.0
    assert log["adjustment"] == 0.0


def test_heart_health_no_calorie_change():
    # DGA 2025-2030 page 3: cardiovascular risk driven by macros, not total cals.
    adjusted, log = _apply_goal_calorie_adjustment(2200.0, "heart_health", None)
    assert adjusted == 2200.0
    assert log["adjustment"] == 0.0


# ── unknown / missing goal ─────────────────────────────────────────────────


def test_unknown_goal_falls_back_to_no_change():
    adjusted, log = _apply_goal_calorie_adjustment(2200.0, "bulking", None)
    assert adjusted == 2200.0
    assert log["adjustment"] == 0.0


def test_missing_goal_treated_as_maintenance():
    adjusted, log = _apply_goal_calorie_adjustment(2200.0, None, None)
    assert adjusted == 2200.0
    assert log["adjustment"] == 0.0


def test_goal_case_and_whitespace_normalised():
    adjusted_a, _ = _apply_goal_calorie_adjustment(2200.0, "Weight Loss", None)
    adjusted_b, _ = _apply_goal_calorie_adjustment(2200.0, "  WEIGHT_LOSS ", None)
    assert adjusted_a == 1700.0
    assert adjusted_b == 1700.0


# ── safety floor ───────────────────────────────────────────────────────────


def test_floor_applied_when_adjustment_drops_below_1200():
    # 1500 maintenance + weight_loss (-500) would land at 1000 — must be capped.
    adjusted, log = _apply_goal_calorie_adjustment(1500.0, "weight_loss", None)
    assert adjusted == _GOAL_CALORIE_FLOOR
    assert log["floor_applied"] is True


def test_floor_not_applied_when_above_threshold():
    adjusted, log = _apply_goal_calorie_adjustment(2000.0, "weight_loss", None)
    assert adjusted == 1500.0
    assert log["floor_applied"] is False


def test_floor_does_not_lift_already_low_maintenance_with_no_adjustment():
    # If maintenance is genuinely below the floor and no adjustment applies,
    # we leave it alone — the floor is a guard against our adjustment going
    # too low, not a general minimum we impose on caller-supplied values.
    adjusted, log = _apply_goal_calorie_adjustment(1000.0, "maintenance", None)
    assert adjusted == 1000.0
    assert log["floor_applied"] is False


# ── degenerate inputs ──────────────────────────────────────────────────────


def test_none_target_passes_through():
    adjusted, log = _apply_goal_calorie_adjustment(None, "weight_loss", None)
    assert adjusted is None
    assert log["adjustment"] == 0.0


def test_zero_target_passes_through():
    adjusted, _ = _apply_goal_calorie_adjustment(0.0, "weight_loss", None)
    assert adjusted == 0.0


def test_negative_target_passes_through():
    adjusted, _ = _apply_goal_calorie_adjustment(-100.0, "weight_loss", None)
    assert adjusted == -100.0


# ── population gating (DGA pages 6-9) ──────────────────────────────────────


def test_pregnancy_skips_adjustment():
    adjusted, log = _apply_goal_calorie_adjustment(
        2200.0, "weight_loss", ["Pregnancy"]
    )
    assert adjusted == 2200.0
    assert log["skipped_population"] == "pregnan"
    assert log["adjustment"] == 0.0


def test_pregnant_substring_match():
    adjusted, log = _apply_goal_calorie_adjustment(
        2200.0, "weight_loss", ["Currently pregnant"]
    )
    assert adjusted == 2200.0
    assert log["skipped_population"] == "pregnan"


def test_lactation_skips_adjustment():
    adjusted, log = _apply_goal_calorie_adjustment(
        2200.0, "weight_loss", ["Lactating"]
    )
    assert adjusted == 2200.0
    assert log["skipped_population"] == "lactat"


def test_breastfeeding_skips_adjustment():
    adjusted, log = _apply_goal_calorie_adjustment(
        2200.0, "muscle_gain", ["Breastfeeding"]
    )
    assert adjusted == 2200.0
    assert log["skipped_population"] == "breastfeed"


def test_unrelated_condition_does_not_skip():
    # Diabetes is a chronic condition but we don't auto-gate on it in v1
    # (DGA defers to clinician — TODO list). Adjustment still runs.
    adjusted, log = _apply_goal_calorie_adjustment(
        2200.0, "weight_loss", ["Type 2 Diabetes"]
    )
    assert adjusted == 1700.0
    assert log["skipped_population"] is None


def test_empty_conditions_list_does_not_skip():
    adjusted, log = _apply_goal_calorie_adjustment(2200.0, "weight_loss", [])
    assert adjusted == 1700.0
    assert log["skipped_population"] is None


def test_none_conditions_does_not_skip():
    adjusted, log = _apply_goal_calorie_adjustment(2200.0, "weight_loss", None)
    assert adjusted == 1700.0
    assert log["skipped_population"] is None


def test_age_under_18_skips_adjustment():
    adjusted, log = _apply_goal_calorie_adjustment(
        2200.0, "weight_loss", None, age_years=17
    )
    assert adjusted == 2200.0
    assert log["skipped_population"] == "skipped_age_population"
    assert log["adjustment"] == 0.0


def test_age_over_65_skips_adjustment():
    adjusted, log = _apply_goal_calorie_adjustment(
        2200.0, "muscle_gain", None, age_years=66
    )
    assert adjusted == 2200.0
    assert log["skipped_population"] == "skipped_age_population"
    assert log["adjustment"] == 0.0


def test_age_in_adult_range_allows_adjustment():
    adjusted, log = _apply_goal_calorie_adjustment(
        2200.0, "weight_loss", None, age_years=30
    )
    assert adjusted == 1700.0
    assert log["skipped_population"] is None


def test_derive_age_uses_explicit_age_first():
    assert _derive_age_years(age=29, date_of_birth="1980-01-01") == 29


def test_derive_age_from_dob():
    assert _derive_age_years(date_of_birth="2000-04-28", today=date(2026, 4, 28)) == 26


def test_derive_age_from_iso_dob():
    assert (
        _derive_age_years(
            date_of_birth="2000-04-28T00:00:00Z",
            today=date(2026, 4, 28),
        )
        == 26
    )


def test_derive_age_invalid_inputs_return_none():
    assert _derive_age_years(age=-1) is None
    assert _derive_age_years(date_of_birth="not-a-date") is None


# ── log payload shape ──────────────────────────────────────────────────────


def test_log_payload_has_all_observability_keys():
    _, log = _apply_goal_calorie_adjustment(2200.0, "weight_loss", None)
    assert set(log.keys()) == {
        "raw_target",
        "health_goal",
        "adjustment",
        "adjusted_target",
        "floor_applied",
        "skipped_population",
    }
    assert log["raw_target"] == 2200.0
    assert log["health_goal"] == "weight_loss"
    assert log["adjusted_target"] == 1700.0


# ── source-of-truth sanity ─────────────────────────────────────────────────


def test_goal_table_matches_sourced_values():
    # Guard against drift: if anyone tweaks these, they must update sources too.
    assert _GOAL_CALORIE_ADJUSTMENT["weight_loss"] == -500.0
    assert _GOAL_CALORIE_ADJUSTMENT["muscle_gain"] == 300.0
    assert _GOAL_CALORIE_ADJUSTMENT["maintenance"] == 0.0
    assert _GOAL_CALORIE_ADJUSTMENT["heart_health"] == 0.0
    assert _GOAL_CALORIE_FLOOR == 1200.0
