from __future__ import annotations

from rag_pipeline.validation.response_validator import (
    _collect_forbidden_terms,
    _keyword_check,
    validate_response,
)


def test_response_validator_collect_forbidden_terms_from_entities_and_config():
    terms = _collect_forbidden_terms(
        {"exclude_ingredient": ["peanut"]},
        {"forbidden_extra_terms": ["shellfish"]},
    )
    assert "peanut" in terms and "shellfish" in terms


def test_response_validator_warn_reject_truncate_actions():
    cfg_warn = {"enabled": True, "action": "warn"}
    ok, violations, text = validate_response("avoid peanut butter", {"exclude_ingredient": ["peanut"]}, cfg_warn)
    assert ok is False and violations
    assert "verify allergens" in text.lower()

    cfg_reject = {"enabled": True, "action": "reject"}
    ok2, _, text2 = validate_response("peanut", {"exclude_ingredient": ["peanut"]}, cfg_reject)
    assert ok2 is False
    assert "couldn't safely generate" in text2.lower()


# ── New gap-fill tests ─────────────────────────────────────────────────────────

def test_validate_response_passes_clean_text():
    """Response with no forbidden terms should pass without modification."""
    cfg = {"enabled": True, "action": "warn"}
    ok, violations, text = validate_response(
        "Try a tofu curry with lentils",
        {"exclude_ingredient": ["peanut"]},
        cfg,
    )
    assert ok is True
    assert violations == []
    assert text == "Try a tofu curry with lentils"


def test_validate_response_disabled_config_always_passes():
    """When enabled=False the validator short-circuits and returns valid."""
    cfg = {"enabled": False, "action": "reject"}
    ok, violations, text = validate_response(
        "Use peanut butter",
        {"exclude_ingredient": ["peanut"]},
        cfg,
    )
    assert ok is True
    assert violations == []


def test_validate_response_empty_response_no_crash():
    """Empty string response should not raise and should be treated as clean."""
    cfg = {"enabled": True, "action": "warn"}
    ok, violations, text = validate_response("", {"exclude_ingredient": ["peanut"]}, cfg)
    assert ok is True
    assert violations == []


def test_validate_response_multiple_violations_all_caught():
    """All forbidden terms should be reported, not just the first one."""
    cfg = {"enabled": True, "action": "warn"}
    ok, violations, text = validate_response(
        "Use peanut butter and shellfish in this dish",
        {"exclude_ingredient": ["peanut", "shellfish"]},
        cfg,
    )
    assert ok is False
    assert "peanut" in violations
    assert "shellfish" in violations


def test_validate_response_case_insensitive_match():
    """Forbidden term matching should be case-insensitive."""
    cfg = {"enabled": True, "action": "warn"}
    ok, violations, _ = validate_response(
        "Add PEANUT BUTTER to the sauce",
        {"exclude_ingredient": ["peanut"]},
        cfg,
    )
    assert ok is False
    assert "peanut" in violations


def test_keyword_check_empty_forbidden_terms_always_valid():
    """No forbidden terms means nothing to check — always passes."""
    valid, violated = _keyword_check("any text whatsoever", [])
    assert valid is True
    assert violated == []


def test_keyword_check_word_boundary_detects_term():
    """'peanut' in text should be detected."""
    valid, violated = _keyword_check("avoid peanut products", ["peanut"])
    assert valid is False
    assert "peanut" in violated
