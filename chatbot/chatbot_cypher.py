"""
Fixed Cypher queries for chatbot intents: show_meal_plan, meal_history, nutrition_summary.

Uses b2c_customer_id property on nodes (no Customer → MealPlan/MealLog relationships).
Deterministic: intent → fixed query. No LLM or dynamic query generation.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from neo4j import Driver

logger = logging.getLogger(__name__)


# ── show_meal_plan ─────────────────────────────────────────────────────────

SHOW_MEAL_PLAN_CYPHER = """
MATCH (mp:MealPlan {b2c_customer_id: $customer_id})
WHERE (mp.status = 'active' OR mp.status IS NULL)
  AND date() >= mp.start_date
  AND date() <= mp.end_date
OPTIONAL MATCH (mp)-[:HAS_ITEM]->(mpi:MealPlanItem)
OPTIONAL MATCH (r:Recipe)
WHERE r.id = mpi.recipe_id
RETURN mp.id AS plan_id, mp.name AS plan_name,
       mp.start_date, mp.end_date,
       mpi.day_index, mpi.meal_type, mpi.recipe_id,
       r.title AS recipe_title
ORDER BY mpi.day_index ASC, mpi.meal_type ASC
"""


def run_show_meal_plan(
    driver: Driver,
    customer_id: str,
    database: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch active meal plan for customer (date range includes today)."""
    try:
        with driver.session(database=database) as session:
            result = session.run(SHOW_MEAL_PLAN_CYPHER, customer_id=customer_id)
            return [dict(record) for record in result]
    except Exception as e:
        logger.warning("show_meal_plan Cypher failed: %s", e)
        return []


def format_meal_plan_response(rows: list[dict[str, Any]]) -> tuple[str, dict[str, Any] | None]:
    """
    Format meal plan rows into human-readable text.
    Returns (response_text, structured_data for nutrition_data field or None).
    """
    if not rows:
        return "You don't have an active meal plan for this week.", None

    # First row has plan metadata (same for all)
    first = rows[0]
    plan_name = first.get("plan_name") or "Your plan"
    start = first.get("start_date")
    end = first.get("end_date")
    date_range = f"{start} to {end}" if start and end else "this week"

    # Group by day_index
    by_day: dict[int, list[dict]] = {}
    for r in rows:
        day_idx = r.get("day_index")
        if day_idx is None:
            continue
        meal_type = r.get("meal_type") or "meal"
        title = r.get("recipe_title") or r.get("custom_name") or "—"
        by_day.setdefault(day_idx, []).append({"meal_type": meal_type, "title": title})

    lines = [f"**{plan_name}** ({date_range}):"]
    for day_idx in sorted(by_day.keys()):
        items = by_day[day_idx]
        day_label = f"Day {day_idx + 1}" if day_idx is not None else "Day"
        meal_strs = [f"{m['meal_type']}: {m['title']}" for m in items]
        lines.append(f"  {day_label}: " + " | ".join(meal_strs))

    return "\n".join(lines), {"plan_name": plan_name, "items_by_day": by_day}


# ── meal_history ───────────────────────────────────────────────────────────

MEAL_HISTORY_CYPHER = """
MATCH (ml:MealLog {b2c_customer_id: $customer_id})
WHERE ml.log_date = $target_date
OPTIONAL MATCH (ml)-[:CONTAINS_ITEM]->(mli:MealLogItem)
OPTIONAL MATCH (r:Recipe)
WHERE r.id = mli.recipe_id
RETURN ml.log_date, ml.total_calories, ml.total_protein_g, ml.total_carbs_g, ml.total_fat_g,
       mli.meal_type, mli.custom_name, r.title AS recipe_title, mli.calories
ORDER BY mli.meal_type ASC
"""


def run_meal_history(
    driver: Driver,
    customer_id: str,
    target_date: date | None = None,
    database: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch meal log for customer for target_date (default: today)."""
    if target_date is None:
        target_date = date.today()
    try:
        with driver.session(database=database) as session:
            result = session.run(
                MEAL_HISTORY_CYPHER,
                customer_id=customer_id,
                target_date=target_date,
            )
            return [dict(record) for record in result]
    except Exception as e:
        logger.warning("meal_history Cypher failed: %s", e)
        return []


def format_meal_history_response(rows: list[dict[str, Any]]) -> tuple[str, dict[str, Any] | None]:
    """Format meal history rows into human-readable text."""
    if not rows:
        return "You haven't logged any meals for today.", None

    items: list[dict[str, Any]] = []
    total_cals = None
    total_protein = None

    for r in rows:
        if total_cals is None and r.get("total_calories") is not None:
            total_cals = r.get("total_calories")
        if total_protein is None and r.get("total_protein_g") is not None:
            total_protein = r.get("total_protein_g")
        meal_type = r.get("meal_type")
        title = r.get("recipe_title") or r.get("custom_name")
        if meal_type or title:
            items.append({"meal_type": meal_type or "meal", "title": title or "—"})

    lines = ["**Today you had:**"]
    for item in items:
        lines.append(f"  • {item['meal_type']}: {item['title']}")
    if total_cals is not None:
        lines.append(f"\nTotal calories: {total_cals}")
    if total_protein is not None:
        lines.append(f"Total protein: {total_protein} g")

    return "\n".join(lines), {"items": items, "total_calories": total_cals, "total_protein_g": total_protein}


# ── nutrition_summary ──────────────────────────────────────────────────────

NUTRITION_SUMMARY_CYPHER = """
MATCH (ml:MealLog {b2c_customer_id: $customer_id})
WHERE ml.log_date >= date() - duration({days: $days})
  AND ml.log_date <= date()
RETURN sum(ml.total_calories) AS total_calories,
       sum(ml.total_protein_g) AS total_protein_g,
       sum(ml.total_carbs_g) AS total_carbs_g,
       sum(ml.total_fat_g) AS total_fat_g,
       count(DISTINCT ml.log_date) AS days_logged
"""


def run_nutrition_summary(
    driver: Driver,
    customer_id: str,
    days: int = 7,
    database: str | None = None,
) -> dict[str, Any] | None:
    """Fetch aggregated nutrition for last N days. Returns single row as dict."""
    try:
        with driver.session(database=database) as session:
            result = session.run(
                NUTRITION_SUMMARY_CYPHER,
                customer_id=customer_id,
                days=days,
            )
            record = result.single()
            return dict(record) if record else None
    except Exception as e:
        logger.warning("nutrition_summary Cypher failed: %s", e)
        return None


def format_nutrition_summary_response(data: dict[str, Any] | None) -> tuple[str, dict[str, Any] | None]:
    """Format nutrition summary into human-readable text."""
    if not data:
        return "No nutrition data logged in the last week.", None

    days = data.get("days_logged") or 0
    if days == 0:
        return "No nutrition data logged in the last week.", None

    total_cals = data.get("total_calories") or 0
    total_protein = data.get("total_protein_g") or 0
    total_carbs = data.get("total_carbs_g") or 0
    total_fat = data.get("total_fat_g") or 0
    avg_cals = round(total_cals / days, 0) if days else 0
    avg_protein = round(total_protein / days, 1) if days else 0

    lines = [
        f"**This week** ({days} days logged):",
        f"  • Total calories: {total_cals}",
        f"  • Average per day: ~{avg_cals} cal",
        f"  • Protein: {total_protein} g total (~{avg_protein} g/day)",
        f"  • Carbs: {total_carbs} g | Fat: {total_fat} g",
    ]
    return "\n".join(lines), {
        "total_calories": total_cals,
        "total_protein_g": total_protein,
        "total_carbs_g": total_carbs,
        "total_fat_g": total_fat,
        "days_logged": days,
    }
