"""Tests for merge_profile_into_entities B2C context behavior."""

from rag_pipeline.orchestrator.profile_enrichment import (
    _derive_cal_upper_limit_from_daily,
    merge_profile_into_entities,
)


def test_merge_exclude_recipe_ids_unions_with_recent_meal_ids():
    entities = {"exclude_recipe_ids": ["recipe-a", "recipe-b"]}
    profile = {
        "context": {
            "recentMealIds": ["recipe-b", "recipe-c"],
        }
    }
    out = merge_profile_into_entities(entities, profile)
    assert set(out["exclude_recipe_ids"]) == {"recipe-a", "recipe-b", "recipe-c"}
    # Order: existing first, then new
    assert out["exclude_recipe_ids"][0] == "recipe-a"
    assert out["exclude_recipe_ids"][-1] == "recipe-c"


def test_recent_meal_ids_empty_preserves_existing_excludes():
    entities = {"exclude_recipe_ids": ["x"]}
    profile = {"context": {"recentMealIds": []}}
    out = merge_profile_into_entities(entities, profile)
    assert out["exclude_recipe_ids"] == ["x"]


def test_target_calories_sets_cal_upper_limit_when_not_preset():
    entities = {}
    profile = {"context": {"targetCalories": 2000}}
    out = merge_profile_into_entities(entities, profile)
    assert out["calorie_target"] == 2000
    # 2000 / 3 * 1.1 ≈ 733
    assert out["cal_upper_limit"] == 733


def test_target_calories_respects_meals_per_day_in_context():
    entities = {}
    profile = {
        "context": {
            "targetCalories": 2000,
            "mealsPerDay": 4,
        }
    }
    out = merge_profile_into_entities(entities, profile)
    # 2000/4 * 1.1 = 550
    assert out["cal_upper_limit"] == 550


def test_explicit_cal_upper_limit_not_overwritten_by_target_calories():
    entities = {"cal_upper_limit": 600}
    profile = {"context": {"targetCalories": 2000}}
    out = merge_profile_into_entities(entities, profile)
    assert out["calorie_target"] == 2000
    assert out["cal_upper_limit"] == 600


def test_derive_cal_upper_limit_returns_none_for_invalid():
    assert _derive_cal_upper_limit_from_daily(None) is None
    assert _derive_cal_upper_limit_from_daily(-1) is None
    assert _derive_cal_upper_limit_from_daily("not a number") is None
