"""
PRD-29: Auto notification content generation.

Generates notification copy (title, body, action_url, icon, type) from
trigger_type + meal_log_summary + health_profile. Template-first, no LLM by default.
Backend calls /recommend/meal-candidates for suggest_breakfast/suggest_lunch
and passes suggested_recipe in meal_log_summary.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Trigger types and their templates. Use {placeholder} for interpolation.
TEMPLATES: dict[str, dict[str, str]] = {
    "missed_breakfast": {
        "title": "Log breakfast",
        "body": "Good morning! You haven't logged breakfast yet — want to log it now?",
        "action_url": "/meal-log",
        "icon": "🍳",
        "type": "meal",
    },
    "missed_lunch": {
        "title": "Log lunch",
        "body": "Looks like you've been busy! Tap here to quickly log your lunch",
        "action_url": "/meal-log",
        "icon": "🥪",
        "type": "meal",
    },
    "high_fat_2day": {
        "title": "Fat intake high",
        "body": "Your fat intake has been trending high — here are some lighter alternatives for today",
        "action_url": "/recipes?diet=low-fat",
        "icon": "📊",
        "type": "nutrition",
    },
    "low_protein_3day": {
        "title": "Low protein",
        "body": "You've been getting less protein than your goal — try adding these high-protein options",
        "action_url": "/recipes?diet=high-protein",
        "icon": "💪",
        "type": "nutrition",
    },
    "no_water": {
        "title": "Stay hydrated",
        "body": "Stay hydrated! You haven't tracked water today",
        "action_url": "/meal-log",
        "icon": "💧",
        "type": "nutrition",
    },
    "streak_milestone": {
        "title": "Streak!",
        "body": "🔥 Amazing! {streak}-day streak! Keep it going!",
        "action_url": "/meal-log",
        "icon": "🔥",
        "type": "engagement",
    },
    "streak_broken": {
        "title": "Start fresh",
        "body": "Your {streak}-day streak ended yesterday — log today to start a new one!",
        "action_url": "/meal-log",
        "icon": "📅",
        "type": "engagement",
    },
    "calorie_overshoot_3day": {
        "title": "Calorie goal",
        "body": "You've been over your calorie goal this week — would you like to adjust your target or explore lighter meals?",
        "action_url": "/recipes",
        "icon": "📉",
        "type": "nutrition",
    },
    "suggest_breakfast": {
        "title": "Breakfast idea",
        "body": "Good morning! How about **{recipe_title}** for breakfast? It's high in protein and matches your goals",
        "action_url": "/recipes/{recipe_id}",
        "icon": "🍳",
        "type": "meal",
    },
    "suggest_lunch": {
        "title": "Lunch idea",
        "body": "Lunchtime! Based on your goals, try **{recipe_title}** — it fits within your calorie budget",
        "action_url": "/recipes/{recipe_id}",
        "icon": "🥗",
        "type": "meal",
    },
    "default": {
        "title": "Nutrition reminder",
        "body": "Stay on track with your health goals — log your meals and stay hydrated.",
        "action_url": "/meal-log",
        "icon": "📋",
        "type": "system",
    },
}

# Fallback bodies when suggest_breakfast/suggest_lunch have no suggested_recipe
SUGGEST_FALLBACK = {
    "suggest_breakfast": {
        "body": "Good morning! How about a high-protein breakfast today? Tap to explore options",
        "action_url": "/recipes?meal=breakfast",
    },
    "suggest_lunch": {
        "body": "Lunchtime! Tap to explore recipes that fit your calorie budget",
        "action_url": "/recipes?meal=lunch",
    },
}


def _extract_interpolation_data(
    trigger_type: str,
    meal_log_summary: dict[str, Any],
    health_profile: dict[str, Any],
) -> dict[str, Any]:
    """Extract placeholder values for template interpolation."""
    data: dict[str, Any] = {}
    summary = meal_log_summary or {}
    profile = health_profile or {}

    # Streak
    data["streak"] = summary.get("current_streak") or summary.get("streak") or 0

    # Recipe (suggest_breakfast, suggest_lunch)
    suggested = summary.get("suggested_recipe") or {}
    if isinstance(suggested, dict):
        data["recipe_title"] = suggested.get("title") or suggested.get("name") or ""
        data["recipe_id"] = str(suggested.get("id") or suggested.get("recipe_id") or "")
    else:
        data["recipe_title"] = ""
        data["recipe_id"] = ""

    # Nutrition (for optional future interpolation)
    data["avg_fat_g"] = summary.get("avg_total_fat_g") or summary.get("avg_fat_g") or profile.get("target_fat_g") or ""
    data["target_fat_g"] = profile.get("target_fat_g") or ""
    data["avg_protein_g"] = summary.get("avg_protein_g") or profile.get("target_protein_g") or ""
    data["target_protein_g"] = profile.get("target_protein_g") or ""

    return data


def _interpolate(template_str: str, data: dict[str, Any]) -> str:
    """Safely interpolate placeholders. Missing keys become empty string."""
    if not template_str:
        return ""
    result = template_str
    for key, val in data.items():
        placeholder = "{" + key + "}"
        if placeholder in result:
            result = result.replace(placeholder, str(val) if val is not None else "")
    # Remove any remaining {x} placeholders
    result = re.sub(r"\{[^}]+\}", "", result)
    return result.strip()


def generate_notification(
    trigger_type: str,
    meal_log_summary: dict[str, Any],
    health_profile: dict[str, Any],
    timezone: str = "UTC",
) -> dict[str, str]:
    """
    Generate notification copy from templates + interpolation.
    No LLM. Returns { title, body, action_url, icon, type }.
    """
    key = (trigger_type or "").strip().lower() or "default"
    if key not in TEMPLATES:
        key = "default"
    template = TEMPLATES[key].copy()

    data = _extract_interpolation_data(trigger_type or "", meal_log_summary or {}, health_profile or {})

    # For suggest_breakfast / suggest_lunch: fallback if no recipe
    if key in ("suggest_breakfast", "suggest_lunch") and not data.get("recipe_title"):
        fallback = SUGGEST_FALLBACK.get(key, {})
        if fallback:
            template["body"] = fallback.get("body", template["body"])
            template["action_url"] = fallback.get("action_url", template["action_url"])

    title = _interpolate(template["title"], data)
    body = _interpolate(template["body"], data)
    action_url = _interpolate(template["action_url"], data)

    return {
        "title": title or template["title"],
        "body": body or template["body"],
        "action_url": action_url or template["action_url"],
        "icon": template["icon"],
        "type": template["type"],
    }
