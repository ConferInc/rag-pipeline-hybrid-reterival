from __future__ import annotations

from chatbot import nlu


def test_extract_hybrid_rule_match_without_llm(monkeypatch):
    monkeypatch.setattr(nlu, "extract_intent", lambda _m: '{"intent":"greeting","entities":{}}')
    out = nlu.extract_hybrid("find me a vegan dinner recipe")
    assert out.source == "rules"
    assert out.intent == "find_recipe"
    assert "diet" in out.entities


def test_extract_family_context_detects_scope_and_role():
    out = nlu._extract_family_context("find meals for my kids")
    assert out.get("target_member_role") == "child"


def test_expand_query_with_context_substitution_more_options_fallback():
    hist = [
        ("assistant", "You can substitute yogurt with coconut yogurt."),
        ("user", "more options"),
    ]
    out = nlu.extract_hybrid("more options", context={"history": hist})
    assert out.intent == "get_substitution_suggestion"
    assert out.source == "rules"


# ── New gap-fill tests ─────────────────────────────────────────────────────────

def test_greeting_rule_detected():
    out = nlu.extract_hybrid("hello")
    assert out.intent == "greeting"
    assert out.source == "rules"


def test_farewell_rule_detected():
    out = nlu.extract_hybrid("bye")
    assert out.intent == "farewell"
    assert out.source == "rules"


def test_out_of_scope_rule_detected():
    out = nlu.extract_hybrid("what is the weather today")
    assert out.intent == "out_of_scope"
    assert out.source == "rules"


def test_show_meal_plan_rule_detected():
    out = nlu.extract_hybrid("show me my meal plan")
    assert out.intent == "show_meal_plan"
    assert out.source == "rules"


def test_multiple_diet_keywords_extracted():
    out = nlu.extract_hybrid("find me a vegan low-carb dinner recipe")
    assert out.intent == "find_recipe"
    assert out.source == "rules"
    diets = out.entities.get("diet", [])
    assert "Vegan" in diets
    assert "Low-Carb" in diets


def test_family_scope_household_wide():
    result = nlu._extract_family_context("find meals for my whole family")
    assert result.get("family_scope") == "family"


def test_family_scope_child_role():
    result = nlu._extract_family_context("find dinner for my daughter")
    assert result.get("target_member_role") == "child"


def test_family_scope_spouse_role():
    result = nlu._extract_family_context("recommend something for my wife")
    assert result.get("target_member_role") == "primary_adult"


def test_extract_hybrid_returns_nlu_result_shape():
    """Returned object must always have intent, entities, source attributes."""
    out = nlu.extract_hybrid("find pasta recipe")
    assert hasattr(out, "intent")
    assert hasattr(out, "entities")
    assert hasattr(out, "source")
    assert isinstance(out.entities, dict)
