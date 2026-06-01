from __future__ import annotations

from chatbot import chatbot_cypher as cc


def test_chatbot_cypher_handles_empty_entities_without_crash():
    msg, data = cc.format_meal_plan_response([])
    assert "don't have an active meal plan" in msg.lower()
    assert data is None


def test_chatbot_cypher_maps_meal_history_rows():
    msg, data = cc.format_meal_history_response(
        [{"meal_type": "lunch", "recipe_title": "Bowl", "total_calories": 500, "total_protein_g": 20}]
    )
    assert "today you had" in msg.lower()
    assert data["total_calories"] == 500
