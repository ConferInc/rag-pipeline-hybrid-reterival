"""
Routes chatbot intents to concrete actions.

WHY: Read-only intents (find_recipe, show_meal_plan) execute immediately.
Write intents (plan_meals, log_meal, swap_meal) need user confirmation first.
This prevents the bot from accidentally creating meal plans or logging meals.

HOW CONFIRMATION WORKS:
1. User: "Plan my meals for the week"
2. Bot returns action_required=True with confirmation_prompt + pending_action
3. Frontend shows confirmation card with [Yes] [No] buttons
4. User says "yes" → RAG returns action_to_execute; Express executes the write
5. Bot confirms: "Your meal plan has been created!"
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any


class ActionType(Enum):
    READ_ONLY = "read"  # Execute immediately, no confirmation
    WRITE = "write"     # Requires user confirmation before execution


# Map intent → ActionType. Unknown intents default to READ_ONLY (execute, no confirmation).
ACTION_REGISTRY: dict[str, ActionType] = {
    # Read-only — execute immediately
    "find_recipe": ActionType.READ_ONLY,
    "find_recipe_by_pantry": ActionType.READ_ONLY,
    "similar_recipes": ActionType.READ_ONLY,
    "recipes_for_cuisine": ActionType.READ_ONLY,
    "recipes_by_nutrient": ActionType.READ_ONLY,
    "rank_results": ActionType.READ_ONLY,
    "get_nutritional_info": ActionType.READ_ONLY,
    "nutrient_in_foods": ActionType.READ_ONLY,
    "nutrient_category": ActionType.READ_ONLY,
    "compare_foods": ActionType.READ_ONLY,
    "check_diet_compliance": ActionType.READ_ONLY,
    "check_substitution": ActionType.READ_ONLY,
    "get_substitution_suggestion": ActionType.READ_ONLY,
    "show_meal_plan": ActionType.READ_ONLY,
    "meal_history": ActionType.READ_ONLY,
    "nutrition_summary": ActionType.READ_ONLY,
    "dietary_advice": ActionType.READ_ONLY,
    # Write — require confirmation
    "plan_meals": ActionType.WRITE,
    "log_meal": ActionType.WRITE,
    "swap_meal": ActionType.WRITE,
    "grocery_list": ActionType.WRITE,
    "set_preference": ActionType.WRITE,
    # Aliases from extractor
    "create_meal_plan": ActionType.WRITE,
    "modify_meal_plan": ActionType.WRITE,
    "create_grocery_list": ActionType.WRITE,
    "modify_grocery_list": ActionType.WRITE,
    "update_preferences": ActionType.WRITE,
    # Conversational — no action
    "greeting": ActionType.READ_ONLY,
    "help": ActionType.READ_ONLY,
    "farewell": ActionType.READ_ONLY,
    "out_of_scope": ActionType.READ_ONLY,
    "unclear": ActionType.READ_ONLY,
}


# Confirmation phrases — user agreeing to execute pending action
CONFIRMATION_PHRASES = frozenset({
    "yes", "yeah", "yep", "sure", "ok", "okay", "confirm", "confirmed",
    "go ahead", "do it", "please", "proceed", "absolutely", "yup",
})


@dataclass
class ActionOrchestratorResult:
    """Result from route_intent — tells chat handler what to return."""
    action_required: bool
    confirmation_prompt: str | None
    pending_action: dict[str, Any] | None
    response_prefix: str | None  # Suggested response text for action_required case


def route_intent(intent: str, entities: dict[str, Any]) -> ActionOrchestratorResult:
    """
    Determine if intent requires confirmation and build pending_action.

    For WRITE intents: returns action_required=True with confirmation_prompt
    and pending_action (params for Express to execute).
    For READ_ONLY: returns action_required=False.
    """
    action_type = ACTION_REGISTRY.get(intent, ActionType.READ_ONLY)

    if action_type != ActionType.WRITE:
        return ActionOrchestratorResult(
            action_required=False,
            confirmation_prompt=None,
            pending_action=None,
            response_prefix=None,
        )

    # Build pending_action for WRITE intents
    pending = _build_pending_action(intent, entities)
    if not pending:
        return ActionOrchestratorResult(
            action_required=False,
            confirmation_prompt=None,
            pending_action=None,
            response_prefix=None,
        )

    prompt = _build_confirmation_prompt(intent, pending)
    prefix = _build_response_prefix(intent, pending)
    return ActionOrchestratorResult(
        action_required=True,
        confirmation_prompt=prompt,
        pending_action=pending,
        response_prefix=prefix,
    )


def is_confirmation_message(message: str) -> bool:
    """True if the message is a user confirming a pending action."""
    normalized = message.strip().lower()
    return normalized in CONFIRMATION_PHRASES


def _build_pending_action(intent: str, entities: dict[str, Any]) -> dict[str, Any] | None:
    """Build {type, params} for the pending action."""
    today = date.today().isoformat()

    if intent in ("plan_meals", "create_meal_plan"):
        return {
            "type": "plan_meals",
            "params": {
                "date_range": entities.get("date_range", "next week"),
                "meals_per_day": entities.get("meals_per_day", ["breakfast", "lunch", "dinner"]),
            },
        }

    if intent == "log_meal":
        recipe = entities.get("recipe_reference", "your meal")
        meal_type = entities.get("meal_type", "lunch")
        return {
            "type": "log_meal",
            "params": {
                "recipe": _title_case(recipe),
                "meal_type": meal_type,
                "date": today,
            },
        }

    if intent == "swap_meal":
        meal_type = entities.get("meal_type", "dinner")
        return {
            "type": "swap_meal",
            "params": {
                "meal_type": meal_type,
                "date": today,
            },
        }

    if intent in ("grocery_list", "create_grocery_list"):
        return {
            "type": "grocery_list",
            "params": {"items": entities.get("items", [])},
        }

    if intent in ("set_preference", "update_preferences"):
        diets = entities.get("diet", [])
        if isinstance(diets, str):
            diets = [diets]
        return {
            "type": "set_preference",
            "params": {"diet": diets},
        }

    if intent in ("modify_meal_plan", "modify_grocery_list"):
        return {"type": intent, "params": dict(entities)}

    return None


def _build_confirmation_prompt(intent: str, pending: dict[str, Any]) -> str:
    """Human-readable prompt for the confirmation card."""
    params = pending.get("params", {})
    action_type = pending.get("type", intent)

    if action_type == "log_meal":
        recipe = params.get("recipe", "your meal")
        meal_type = params.get("meal_type", "lunch")
        return f"Log {recipe} as your {meal_type} for today?"

    if action_type == "plan_meals":
        return "Create a meal plan for the next week (breakfast, lunch, dinner)?"

    if action_type == "swap_meal":
        meal_type = params.get("meal_type", "dinner")
        return f"Swap today's {meal_type} for a different recipe?"

    if action_type == "grocery_list":
        return "Add these items to your grocery list?"

    if action_type == "set_preference":
        diets = params.get("diet", [])
        diet_str = ", ".join(diets) if diets else "your preferences"
        return f"Update your dietary preferences to {diet_str}?"

    return f"Confirm this action: {action_type}?"


def _build_response_prefix(intent: str, pending: dict[str, Any]) -> str:
    """Suggested bot response when asking for confirmation."""
    params = pending.get("params", {})
    action_type = pending.get("type", intent)

    if action_type == "log_meal":
        recipe = params.get("recipe", "your meal")
        meal_type = params.get("meal_type", "lunch")
        return f"I'll log {recipe} as your {meal_type} for today. Shall I go ahead?"

    if action_type == "plan_meals":
        return "I'll create a meal plan for the next week. Shall I go ahead?"

    if action_type == "swap_meal":
        meal_type = params.get("meal_type", "dinner")
        return f"I'll swap today's {meal_type} for a different recipe. Shall I go ahead?"

    if action_type == "grocery_list":
        return "I'll add these items to your grocery list. Shall I go ahead?"

    if action_type == "set_preference":
        return "I'll update your dietary preferences. Shall I go ahead?"

    return "Shall I go ahead?"


def _title_case(s: str) -> str:
    """Simple title case for recipe names."""
    if not s:
        return s
    return " ".join(w.capitalize() for w in s.split())
