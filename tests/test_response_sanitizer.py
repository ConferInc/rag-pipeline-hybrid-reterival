#!/usr/bin/env python3
"""Unit tests for response_sanitizer."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag_pipeline.augmentation.response_sanitizer import (
    sanitize_response,
    SANITIZE_INTENTS,
)


def test_allergen_redaction():
    """Allergen in response should be redacted."""
    profile = {"allergens": ["peanut"], "diets": []}
    text = "You can add peanut butter for extra flavor."
    out = sanitize_response(
        text, profile, intent="find_recipe",
        append_disclaimer=False,
    )
    assert "peanut" not in out.lower()
    assert "[removed" in out


def test_safe_context_peanut_free_not_redacted():
    """'peanut-free' should NOT be redacted (safe context)."""
    profile = {"allergens": ["peanut"], "diets": []}
    text = "This recipe is peanut-free and safe for you."
    out = sanitize_response(
        text, profile, intent="find_recipe",
        append_disclaimer=False,
    )
    assert "peanut-free" in out


def test_diet_violation_redaction():
    """Vegan diet: 'chicken' should be redacted."""
    profile = {"allergens": [], "diets": ["Vegan"]}
    text = "Try adding chicken or tofu for protein."
    out = sanitize_response(
        text, profile, intent="find_recipe",
        append_disclaimer=False,
    )
    assert "chicken" not in out.lower()
    assert "[removed" in out


def test_empty_profile_no_change():
    """Empty profile should return text unchanged."""
    text = "Add peanut butter and chicken."
    out = sanitize_response(text, None, intent="find_recipe")
    assert out == text

    out2 = sanitize_response(text, {}, intent="find_recipe")
    assert out2 == text


def test_non_sanitize_intent_skipped():
    """Intent not in SANITIZE_INTENTS should skip sanitization."""
    profile = {"allergens": ["peanut"], "diets": []}
    text = "Add peanut butter."
    out = sanitize_response(text, profile, intent="greeting", append_disclaimer=False)
    assert out == text


def test_disclaimer_appended_when_modified():
    """When redaction occurs, disclaimer should be appended (if enabled)."""
    profile = {"allergens": ["peanut"], "diets": []}
    text = "Use peanut sauce."
    out = sanitize_response(
        text, profile, intent="find_recipe",
        append_disclaimer=True,
        disclaimer="\n\n_Verify allergens._",
    )
    assert "_Verify allergens._" in out


def test_config_override():
    """Config can override redact_allergens."""
    profile = {"allergens": ["peanut"], "diets": []}
    text = "Add peanut butter."
    config = {"redact_allergens": False}
    out = sanitize_response(text, profile, intent="find_recipe", config=config, append_disclaimer=False)
    assert out == text


def test_aggregated_profile():
    """Profile with multiple allergens/diets works (aggregated household)."""
    profile = {
        "allergens": ["peanut", "shellfish"],
        "diets": ["Vegan"],
    }
    text = "You could use shrimp and peanuts, or try tofu instead."
    out = sanitize_response(
        text, profile, intent="find_recipe",
        append_disclaimer=False,
    )
    assert "shrimp" not in out.lower()
    assert "peanut" not in out.lower()
    assert "tofu" in out


if __name__ == "__main__":
    test_allergen_redaction()
    test_safe_context_peanut_free_not_redacted()
    test_diet_violation_redaction()
    test_empty_profile_no_change()
    test_non_sanitize_intent_skipped()
    test_disclaimer_appended_when_modified()
    test_config_override()
    test_aggregated_profile()
    print("All response_sanitizer tests passed.")
