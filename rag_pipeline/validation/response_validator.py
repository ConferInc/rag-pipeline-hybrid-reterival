"""
Response post-validation: hybrid approach (keyword blacklist + optional LLM second-pass).

Validates that the LLM response does not recommend or mention forbidden items
(allergens, excluded ingredients). Uses keyword check first; optional LLM validation for high-stakes.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


def _collect_forbidden_terms(entities: dict[str, Any], config: dict[str, Any]) -> list[str]:
    """Build list of forbidden terms from entities and config."""
    terms: list[str] = []
    # Excluded ingredients from extractor
    excluded = entities.get("exclude_ingredient") or []
    if isinstance(excluded, list):
        terms.extend(str(x).strip() for x in excluded if x)
    elif excluded:
        terms.append(str(excluded).strip())
    # Extra terms from config (e.g. common allergens)
    extra = config.get("forbidden_extra_terms") or []
    if isinstance(extra, list):
        terms.extend(str(x).strip() for x in extra if x)
    # Normalize: lowercase for matching, keep originals for reporting
    return [t for t in terms if t]


def _keyword_check(response_text: str, forbidden_terms: list[str]) -> tuple[bool, list[str]]:
    """
    Check if response contains any forbidden terms (case-insensitive substring).

    Returns:
        (is_valid, list of violated terms)
    """
    if not forbidden_terms:
        return True, []
    text_lower = response_text.lower()
    violated: list[str] = []
    for term in forbidden_terms:
        # Word-boundary aware: "peanut" matches "peanuts", "peanut butter"
        pattern = r"\b" + re.escape(term.lower()) + r"s?\b"
        if re.search(pattern, text_lower):
            violated.append(term)
        elif term.lower() in text_lower:
            violated.append(term)
    return len(violated) == 0, violated


def validate_response(
    response_text: str,
    entities: dict[str, Any],
    config: dict[str, Any],
) -> tuple[bool, list[str], str]:
    """
    Hybrid validation: keyword blacklist first, optional LLM second-pass.

    Args:
        response_text: LLM response to validate
        entities: Extracted entities (exclude_ingredient, etc.)
        config: response_validation section from embedding_config.yaml

    Returns:
        (is_valid, violations, final_response)
        - is_valid: True if no forbidden items detected
        - violations: list of violated terms (if any)
        - final_response: original or with disclaimer appended when action is 'warn'
    """
    if not config.get("enabled", False):
        return True, [], response_text

    forbidden = _collect_forbidden_terms(entities, config)
    if not forbidden:
        return True, [], response_text

    is_valid, violations = _keyword_check(response_text, forbidden)
    action = config.get("action", "warn")  # warn | truncate | reject

    if is_valid:
        return True, [], response_text

    logger.warning(
        "Response validation: forbidden terms detected",
        extra={
            "component": "validation",
            "violations": violations,
            "action": action,
        },
    )

    if action == "reject":
        fallback = config.get("reject_fallback_message") or (
            "I couldn't safely generate a recommendation. Please verify any dietary restrictions or allergens."
        )
        return False, violations, fallback

    if action == "truncate":
        # Simple truncation: remove sentences containing violations (best-effort)
        sentences = re.split(r"(?<=[.!?])\s+", response_text)
        safe_sentences = []
        for s in sentences:
            s_lower = s.lower()
            if not any(v.lower() in s_lower for v in violations):
                safe_sentences.append(s)
        truncated = " ".join(safe_sentences).strip()
        if not truncated:
            truncated = config.get("truncate_fallback_message") or (
                "Some content was removed for safety. Please double-check allergens and dietary restrictions."
            )
        return False, violations, truncated

    # action == "warn" (default): append disclaimer
    disclaimer = config.get("warn_disclaimer") or (
        "\n\n_Please verify allergens and dietary restrictions before consuming._"
    )
    return False, violations, response_text + disclaimer
