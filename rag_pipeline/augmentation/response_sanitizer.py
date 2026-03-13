"""
Post-LLM response sanitization for allergen and diet compliance.

Scans LLM-generated text for profile-violating ingredients and redacts or warns.
Used when the LLM might hallucinate recipes outside the database.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Allergen name → common synonyms/variants for blocklist matching
ALLERGEN_SYNONYMS: dict[str, list[str]] = {
    "peanut": ["peanut", "peanuts", "peanut butter", "groundnut", "groundnuts"],
    "tree nut": ["almond", "almonds", "cashew", "cashews", "walnut", "walnuts", "pecan", "pecans", "hazelnut", "hazelnuts", "pistachio", "pistachios", "macadamia", "brazil nut"],
    "nuts": ["nut", "nuts", "peanut", "peanuts", "almond", "cashew", "walnut", "pecan", "hazelnut", "pistachio"],
    "milk": ["milk", "dairy", "cream", "butter", "cheese", "yogurt", "yoghurt", "whey", "casein"],
    "dairy": ["milk", "dairy", "cream", "butter", "cheese", "yogurt", "yoghurt", "whey", "casein"],
    "egg": ["egg", "eggs", "egg whites", "egg yolks"],
    "eggs": ["egg", "eggs", "egg whites", "egg yolks"],
    "fish": ["fish", "salmon", "tuna", "cod", "tilapia", "halibut", "trout", "sardine", "anchovy", "seafood"],
    "shellfish": ["shrimp", "prawn", "crab", "lobster", "scallop", "oyster", "clam", "mussel", "shellfish", "crawfish", "crayfish"],
    "gluten": ["wheat", "barley", "rye", "gluten", "bread", "flour", "pasta", "couscous", "bulgur"],
    "wheat": ["wheat", "wheat flour", "bread flour", "all-purpose flour"],
    "soy": ["soy", "soya", "soybean", "tofu", "tempeh", "edamame", "soy sauce"],
    "sesame": ["sesame", "sesame seeds", "tahini"],
    "mustard": ["mustard", "mustard seeds", "mustard oil"],
    "celery": ["celery", "celery root", "celeriac"],
    "lupin": ["lupin", "lupine", "lupini"],
    "sulfite": ["sulfite", "sulphite", "sulfites", "sulphites"],
}

# Diet → forbidden ingredient keywords (for post-LLM diet filter)
DIET_VIOLATION_KEYWORDS: dict[str, list[str]] = {
    "vegan": ["chicken", "beef", "pork", "lamb", "fish", "salmon", "tuna", "shrimp", "meat", "bacon", "ham", "sausage", "milk", "cheese", "butter", "cream", "egg", "eggs", "honey", "gelatin"],
    "vegetarian": ["chicken", "beef", "pork", "lamb", "fish", "salmon", "tuna", "shrimp", "meat", "bacon", "ham", "sausage", "gelatin"],
    "keto": ["sugar", "bread", "pasta", "rice", "potato", "potatoes", "flour", "honey", "maple syrup", "corn", "oat", "quinoa"],
    "ketogenic": ["sugar", "bread", "pasta", "rice", "potato", "potatoes", "flour", "honey", "maple syrup", "corn", "oat", "quinoa"],
    "paleo": ["bread", "pasta", "rice", "beans", "lentils", "dairy", "cheese", "milk", "soy", "legumes"],
    "gluten-free": ["wheat", "barley", "rye", "bread", "pasta", "flour", "couscous", "bulgur", "seitan"],
    "gluten free": ["wheat", "barley", "rye", "bread", "pasta", "flour", "couscous", "bulgur", "seitan"],
    "dairy-free": ["milk", "cheese", "butter", "cream", "yogurt", "yoghurt", "whey", "casein"],
    "dairy free": ["milk", "cheese", "butter", "cream", "yogurt", "yoghurt", "whey", "casein"],
    "nut-free": ["peanut", "peanuts", "almond", "cashew", "walnut", "pecan", "hazelnut", "pistachio", "nut", "nuts"],
    "nut free": ["peanut", "peanuts", "almond", "cashew", "walnut", "pecan", "hazelnut", "pistachio", "nut", "nuts"],
}

# Intents where response may suggest recipes/ingredients — run sanitizer
SANITIZE_INTENTS: frozenset[str] = frozenset({
    "find_recipe", "find_recipe_by_pantry", "similar_recipes", "recipes_for_cuisine",
    "recipes_by_nutrient", "ingredient_in_recipes", "cuisine_recipes",
    "get_substitution_suggestion", "check_substitution", "get_nutritional_info",
    "compare_foods", "nutrient_in_foods", "ingredient_nutrients", "check_diet_compliance",
    "find_product", "product_nutrients", "general_nutrition",
})


def _expand_allergen_terms(allergens: list[str]) -> set[str]:
    """Expand profile allergens into blocklist terms (incl. synonyms)."""
    terms: set[str] = set()
    for a in allergens:
        if not a or not isinstance(a, str):
            continue
        a_clean = a.strip().lower()
        if not a_clean:
            continue
        terms.add(a_clean)
        # Add known synonyms
        for key, syns in ALLERGEN_SYNONYMS.items():
            if key in a_clean or a_clean in key:
                terms.update(syns)
        # Also add simple plural/singular
        if a_clean.endswith("s"):
            terms.add(a_clean[:-1])
        else:
            terms.add(a_clean + "s")
    return terms


def _expand_diet_violation_terms(diets: list[str]) -> set[str]:
    """Expand profile diets into forbidden ingredient keywords."""
    terms: set[str] = set()
    for d in diets:
        if not d or not isinstance(d, str):
            continue
        d_clean = d.strip().lower()
        if not d_clean:
            continue
        if d_clean in DIET_VIOLATION_KEYWORDS:
            terms.update(DIET_VIOLATION_KEYWORDS[d_clean])
    return terms


def _is_safe_context(text: str, term: str, start: int, end: int) -> bool:
    """
    Return True if the term appears in a 'safe' context (e.g. 'peanut-free', 'without peanuts').
    """
    # Look at ~30 chars before and after
    ctx_start = max(0, start - 30)
    ctx_end = min(len(text), end + 30)
    context = text[ctx_start:ctx_end].lower()
    # Safe patterns: X-free, without X, no X, X-free option, etc.
    safe_patterns = [
        rf"\b{re.escape(term)}\s*[-]?\s*free",
        rf"without\s+{re.escape(term)}",
        rf"no\s+{re.escape(term)}\b",
        rf"omit\s+{re.escape(term)}",
        rf"skip\s+{re.escape(term)}",
        rf"avoid\s+{re.escape(term)}",
        rf"excluding\s+{re.escape(term)}",
        rf"substitute\s+for\s+{re.escape(term)}",
    ]
    for pat in safe_patterns:
        if re.search(pat, context):
            return True
    return False


def _redact_violations(
    text: str,
    blocklist: set[str],
    *,
    replacement: str = "[removed - not suitable for your dietary profile]",
) -> tuple[str, list[str]]:
    """
    Scan text for blocklisted terms (word-boundary) and redact violations.
    Skips safe contexts like 'peanut-free'. Returns (sanitized_text, list of redacted terms).
    """
    if not blocklist or not text:
        return text, []

    redacted_terms: set[str] = set()
    matches_to_replace: list[tuple[int, int, str]] = []

    for term in blocklist:
        if len(term) < 3:
            continue
        pattern = r'\b(' + re.escape(term) + r')\b'
        for m in re.finditer(pattern, text, re.IGNORECASE):
            if _is_safe_context(text, term, m.start(), m.end()):
                continue
            redacted_terms.add(term)
            matches_to_replace.append((m.start(), m.end(), term))

    if not matches_to_replace:
        return text, []

    matches_to_replace.sort(key=lambda x: x[0], reverse=True)
    result = text
    for start, end, _ in matches_to_replace:
        result = result[:start] + replacement + result[end:]

    return result, list(redacted_terms)


def sanitize_response(
    response_text: str,
    profile: dict[str, Any] | None,
    *,
    intent: str | None = None,
    redact_allergens: bool = True,
    redact_diet: bool = True,
    append_disclaimer: bool = True,
    disclaimer: str = "\n\n_Please verify ingredients against your dietary needs and allergens._",
    config: dict[str, Any] | None = None,
) -> str:
    """
    Sanitize LLM response to remove or flag profile-violating ingredients.

    Args:
        response_text: Raw LLM response
        profile: Customer profile with allergens, diets, health_conditions
        intent: Detected intent; sanitizer runs only for SANITIZE_INTENTS
        redact_allergens: Whether to redact allergen mentions
        redact_diet: Whether to redact diet-violating ingredient mentions
        append_disclaimer: Whether to append disclaimer when modifications made
        disclaimer: Disclaimer text to append
        config: Override config dict (from embedding_config response_validation)

    Returns:
        Sanitized response text
    """
    if config is not None:
        redact_allergens = config.get("redact_allergens", redact_allergens)
        redact_diet = config.get("redact_diet", redact_diet)
        append_disclaimer = config.get("append_disclaimer", append_disclaimer)
        disclaimer = config.get("warn_disclaimer", disclaimer)

    if intent and intent not in SANITIZE_INTENTS:
        return response_text

    if not profile:
        return response_text

    allergens = profile.get("allergens") or []
    diets = profile.get("diets") or []

    if not allergens and not diets:
        return response_text

    blocklist: set[str] = set()
    if redact_allergens and allergens:
        blocklist.update(_expand_allergen_terms(allergens))
    if redact_diet and diets:
        blocklist.update(_expand_diet_violation_terms(diets))

    if not blocklist:
        return response_text

    sanitized, redacted = _redact_violations(response_text, blocklist)

    if redacted:
        logger.info(
            "Response sanitized: redacted terms",
            extra={"component": "response_sanitizer", "redacted_terms": redacted[:10]},
        )
        if append_disclaimer and disclaimer and disclaimer not in sanitized:
            sanitized = sanitized.rstrip() + disclaimer

    return sanitized
