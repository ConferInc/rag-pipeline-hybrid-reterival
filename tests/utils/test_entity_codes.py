from __future__ import annotations

from entity_codes import normalize_to_allergen, normalize_to_condition, normalize_to_diet


def test_normalize_aliases_allergen_condition_diet():
    assert normalize_to_allergen("PEANUTS") == "peanut"
    assert normalize_to_condition("high blood pressure") == "hypertension"
    assert normalize_to_diet("keto") == "ketogenic"


def test_normalize_unknown_returns_none():
    assert normalize_to_allergen("mystery allergen") is None
    assert normalize_to_condition("unknown condition") is None
    assert normalize_to_diet("unknown diet") is None
