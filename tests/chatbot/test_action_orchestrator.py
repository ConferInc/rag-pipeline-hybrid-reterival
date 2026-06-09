from __future__ import annotations

import pytest

from chatbot.action_orchestrator import (
    is_confirmation_message,
    is_rejection_message,
    route_intent,
    CONFIRMATION_PHRASES,
    REJECTION_PHRASES,
    ACTION_REGISTRY,
    ActionType,
)


def test_write_intent_requires_confirmation():
    out = route_intent("plan_meals", {})
    assert out.action_required is True
    assert out.pending_action is not None


def test_read_only_intent_executes_immediately():
    out = route_intent("find_recipe", {})
    assert out.action_required is False
    assert out.pending_action is None


def test_confirmation_prefix_guard_avoids_okra_false_positive():
    assert is_confirmation_message("ok, proceed") is True
    assert is_confirmation_message("okra curry recipe") is False


# ── New gap-fill tests ─────────────────────────────────────────────────────────

def test_unknown_intent_defaults_to_read_only():
    """Intents not in ACTION_REGISTRY should fall back to READ_ONLY (no confirmation)."""
    out = route_intent("totally_unknown_intent_xyz", {})
    assert out.action_required is False
    assert out.pending_action is None
    assert out.confirmation_prompt is None


@pytest.mark.parametrize("intent", [
    "log_meal",
    "swap_meal",
    "grocery_list",
    "set_preference",
    "create_meal_plan",
    "modify_meal_plan",
    "create_grocery_list",
    "modify_grocery_list",
    "update_preferences",
])
def test_all_write_intents_require_confirmation(intent):
    """Every WRITE intent must return action_required=True."""
    out = route_intent(intent, {})
    assert ACTION_REGISTRY.get(intent) == ActionType.WRITE
    assert out.action_required is True


@pytest.mark.parametrize("phrase", [
    "no", "nope", "cancel", "nevermind", "never mind",
    "forget it", "nah", "don't", "stop", "not now",
])
def test_all_rejection_phrases_recognized(phrase):
    assert is_rejection_message(phrase) is True


def test_rejection_returns_false_for_neutral_message():
    assert is_rejection_message("maybe") is False
    assert is_rejection_message("") is False


@pytest.mark.parametrize("phrase", [
    "yes", "yeah", "yep", "sure", "ok", "okay",
    "confirm", "confirmed", "proceed", "absolutely", "yup",
])
def test_all_confirmation_phrases_recognized(phrase):
    assert is_confirmation_message(phrase) is True


def test_confirmation_prefix_with_natural_follow_on():
    """'yes, go ahead and log it' — starts with 'yes' + space."""
    assert is_confirmation_message("yes, go ahead and log it") is True
    assert is_confirmation_message("sure, do it please") is True


def test_log_meal_pending_action_structure():
    """log_meal pending_action must have type, params with recipe, meal_type, date."""
    out = route_intent("log_meal", {"recipe_reference": "pasta primavera", "meal_type": "lunch"})
    assert out.action_required is True
    assert out.pending_action["type"] == "log_meal"
    params = out.pending_action["params"]
    assert "recipe" in params
    assert "meal_type" in params
    assert "date" in params
    assert out.pending_action.get("action_id")


def test_plan_meals_confirmation_prompt_contains_meal_plan():
    out = route_intent("plan_meals", {})
    assert out.confirmation_prompt is not None
    assert "meal plan" in out.confirmation_prompt.lower()


def test_is_rejection_case_insensitive_not_required():
    """Rejection phrases are matched exact lowercase — ensure lowercase 'no' works."""
    assert is_rejection_message("no") is True
