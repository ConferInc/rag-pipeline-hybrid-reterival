"""
Contract tests for `MealCandidateRequest.meals_per_day` — Fix 1 transition.

The canonical shape from the frontend/Express stack is a list of meal-type
strings (e.g. ["breakfast", "lunch", "dinner"]). A bare integer count is still
accepted as a transitional fallback for legacy callers.
"""

from __future__ import annotations

import pytest

from api.app import MealCandidateRequest, _normalize_meals_per_day


def test_normalize_returns_count_for_int():
    slots, count = _normalize_meals_per_day(3)
    assert slots is None
    assert count == 3


def test_normalize_returns_slots_and_count_for_list():
    slots, count = _normalize_meals_per_day(["breakfast", "lunch", "dinner"])
    assert slots == ["breakfast", "lunch", "dinner"]
    assert count == 3


def test_normalize_lowercases_and_strips_list_entries():
    slots, count = _normalize_meals_per_day([" Breakfast ", "LUNCH", "Dinner"])
    assert slots == ["breakfast", "lunch", "dinner"]
    assert count == 3


def test_normalize_drops_unknown_slots():
    slots, count = _normalize_meals_per_day(["breakfast", "brunch", "lunch"])
    assert slots == ["breakfast", "lunch"]
    assert count == 2


def test_normalize_empty_list_returns_zero_count():
    slots, count = _normalize_meals_per_day([])
    assert slots is None
    assert count == 0


def test_normalize_list_of_all_invalid_slots_returns_zero_count():
    slots, count = _normalize_meals_per_day(["brunch", "elevenses"])
    assert slots is None
    assert count == 0


def test_normalize_none_returns_zero_count():
    slots, count = _normalize_meals_per_day(None)
    assert slots is None
    assert count == 0


def test_normalize_non_positive_int_returns_zero_count():
    assert _normalize_meals_per_day(0) == (None, 0)
    assert _normalize_meals_per_day(-2) == (None, 0)


def test_normalize_rejects_bool():
    assert _normalize_meals_per_day(True) == (None, 0)
    assert _normalize_meals_per_day(False) == (None, 0)


def test_normalize_rejects_unsupported_type():
    assert _normalize_meals_per_day("breakfast") == (None, 0)
    assert _normalize_meals_per_day({"breakfast": 1}) == (None, 0)


def test_normalize_snack_slot_is_valid():
    slots, count = _normalize_meals_per_day(["breakfast", "snack"])
    assert slots == ["breakfast", "snack"]
    assert count == 2


def test_request_accepts_list_of_meal_types():
    req = MealCandidateRequest(
        customer_id="cust-uuid-123",
        meals_per_day=["breakfast", "lunch", "dinner"],
    )
    assert req.meals_per_day == ["breakfast", "lunch", "dinner"]


def test_request_accepts_legacy_integer():
    req = MealCandidateRequest(customer_id="cust-uuid-123", meals_per_day=3)
    assert req.meals_per_day == 3


def test_request_accepts_none():
    req = MealCandidateRequest(customer_id="cust-uuid-123")
    assert req.meals_per_day is None
