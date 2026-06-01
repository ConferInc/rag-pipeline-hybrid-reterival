from __future__ import annotations

from rag_pipeline.profile import household_profile as hp


def test_household_aggregate_profile_union_and_intersection_rules():
    out = hp.aggregate_profile(
        [
            {"diets": ["Vegan", "Low-Fat"], "allergens": ["peanut"], "health_conditions": ["hypertension"], "recent_recipes": ["A"]},
            {"diets": ["Vegan"], "allergens": ["shellfish"], "health_conditions": ["gerd"], "recent_recipes": ["B"]},
        ]
    )
    assert out["diets"] == ["Vegan"]
    assert set(out["allergens"]) == {"peanut", "shellfish"}


def test_resolve_profile_for_recommendation_role_vs_family_scope(monkeypatch):
    monkeypatch.setattr(hp, "get_household_id_for_customer", lambda *_a, **_k: "hh1")
    monkeypatch.setattr(hp, "resolve_profile_for_role", lambda *_a, **_k: {"diets": ["KidDiet"]})
    monkeypatch.setattr(hp, "_fetch_single_customer_profile", lambda *_a, **_k: {"diets": ["SelfDiet"]})
    monkeypatch.setattr(hp, "get_household_type", lambda *_a, **_k: "family")

    out = hp.resolve_profile_for_recommendation(
        driver=object(),
        customer_id="c1",
        target_member_role="child",
    )
    assert out["diets"] == ["KidDiet"]


# ── New gap-fill tests ─────────────────────────────────────────────────────────

def test_aggregate_profile_empty_member_list_returns_empty_profile():
    """aggregate_profile([]) must return the empty-profile shape without crashing."""
    out = hp.aggregate_profile([])
    assert out["diets"] == []
    assert out["allergens"] == []
    assert out["health_conditions"] == []
    assert out["recent_recipes"] == []


def test_aggregate_profile_single_member_passthrough():
    """Single member — diets and allergens should match that member exactly."""
    member = {
        "diets": ["Keto"],
        "allergens": ["shellfish"],
        "health_conditions": ["hypertension"],
        "recent_recipes": ["recipe-1"],
        "health_goal": "weight_loss",
        "activity_level": "moderate",
    }
    out = hp.aggregate_profile([member])
    assert out["diets"] == ["Keto"]
    assert out["allergens"] == ["shellfish"]
    assert out["health_conditions"] == ["hypertension"]
    assert "recipe-1" in out["recent_recipes"]


def test_aggregate_profile_health_conditions_are_union():
    """health_conditions must be the union across all members."""
    out = hp.aggregate_profile(
        [
            {"diets": [], "allergens": [], "health_conditions": ["diabetes"], "recent_recipes": []},
            {"diets": [], "allergens": [], "health_conditions": ["hypertension"], "recent_recipes": []},
        ]
    )
    assert "diabetes" in out["health_conditions"]
    assert "hypertension" in out["health_conditions"]


def test_aggregate_profile_recent_recipes_are_union():
    """recent_recipes must be the union so that recipes eaten by any member are excluded."""
    out = hp.aggregate_profile(
        [
            {"diets": [], "allergens": [], "health_conditions": [], "recent_recipes": ["r1", "r2"]},
            {"diets": [], "allergens": [], "health_conditions": [], "recent_recipes": ["r2", "r3"]},
        ]
    )
    assert set(out["recent_recipes"]) == {"r1", "r2", "r3"}
