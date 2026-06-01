from __future__ import annotations

from rag_pipeline.augmentation.response_sanitizer import sanitize_response


def test_sanitize_case_insensitive_and_non_violating_unchanged():
    profile = {"allergens": ["peanut"], "diets": []}
    out = sanitize_response("PEANUT sauce is tasty", profile, intent="find_recipe", append_disclaimer=False)
    assert "peanut" not in out.lower()

    clean = sanitize_response("Try tofu and rice", profile, intent="find_recipe", append_disclaimer=False)
    assert clean == "Try tofu and rice"


def test_sanitize_disclaimer_format():
    profile = {"allergens": ["peanut"], "diets": []}
    out = sanitize_response(
        "Use peanut butter",
        profile,
        intent="find_recipe",
        append_disclaimer=True,
        disclaimer="--check safety--",
    )
    assert out.endswith("--check safety--")
