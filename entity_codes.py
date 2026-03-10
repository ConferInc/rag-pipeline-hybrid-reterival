"""
Shared canonical entity codes — allergens, health conditions, diets.
Source: gold.allergens, gold.health_conditions, gold.dietary_preferences.
Used by B2B and B2C NLU for rule-based extraction and LLM output normalization.

Normalization accepts any user phrasing (spaces, dashes, mixed case, punctuation)
and maps to canonical DB codes.
"""

from __future__ import annotations

import re

# ── Allergens (DB codes, snake_case) ──────────────────────────────────────────

ALLOWED_ALLERGENS: frozenset[str] = frozenset({
    "seeds", "other_legumes", "egg", "corn", "sesame", "buckwheat",
    "alpha_gal_syndrome", "tree_nuts", "fish", "spices_herbs", "peanut",
    "insect", "soy", "oral_allergy_syndrome", "celery", "wheat_gluten_cereals",
    "gelatin", "molluscs", "shellfish", "milk_dairy",
})

# Keyword/phrase -> DB code (for heuristic extraction)
ALLERGEN_KEYWORDS: dict[str, str] = {
    "seeds": "seeds",
    "seed": "seeds",
    "legumes": "other_legumes",
    "legume": "other_legumes",
    "bean": "other_legumes",
    "egg": "egg",
    "eggs": "egg",
    "corn": "corn",
    "sesame": "sesame",
    "buckwheat": "buckwheat",
    "alpha-gal": "alpha_gal_syndrome",
    "alpha gal": "alpha_gal_syndrome",
    "alphagal": "alpha_gal_syndrome",
    "tree nut": "tree_nuts",
    "tree nuts": "tree_nuts",
    "nuts": "tree_nuts",
    "fish": "fish",
    "spices": "spices_herbs",
    "herbs": "spices_herbs",
    "peanut": "peanut",
    "peanuts": "peanut",
    "insect": "insect",
    "insects": "insect",
    "soy": "soy",
    "soya": "soy",
    "oral allergy": "oral_allergy_syndrome",
    "oas": "oral_allergy_syndrome",
    "celery": "celery",
    "wheat": "wheat_gluten_cereals",
    "gluten": "wheat_gluten_cereals",
    "cereals": "wheat_gluten_cereals",
    "gelatin": "gelatin",
    "mollusc": "molluscs",
    "molluscs": "molluscs",
    "shellfish": "shellfish",
    "milk": "milk_dairy",
    "dairy": "milk_dairy",
    "lactose": "milk_dairy",
}

# User phrasing variations -> canonical code (handles mixed case, punctuation, etc.)
ALLERGEN_ALIASES: dict[str, str] = {
    "peanuts": "peanut",
    "tree nut": "tree_nuts",
    "tree_nut": "tree_nuts",
    "tree nuts": "tree_nuts",
    "milk": "milk_dairy",
    "eggs": "egg",
    "spices and herbs": "spices_herbs",
    "spices_herbs": "spices_herbs",
    "spices & herbs": "spices_herbs",
    "wheat/gluten cereals": "wheat_gluten_cereals",
    "wheat_gluten": "wheat_gluten_cereals",
    "wheat gluten": "wheat_gluten_cereals",
    "gluten cereals": "wheat_gluten_cereals",
    "milk (dairy)": "milk_dairy",
    "no dairy": "milk_dairy",
    "no peanuts": "peanut",
    "no nuts": "tree_nuts",
    "no gluten": "wheat_gluten_cereals",
    "no wheat": "wheat_gluten_cereals",
}

# ── Health conditions (DB codes) ──────────────────────────────────────────────

ALLOWED_CONDITIONS: frozenset[str] = frozenset({
    "food_allergy_other", "diabetics_type_2", "hyperlipidemia", "kidney_disease",
    "liver_disease", "non_celiac_gluten_sensitivity", "type_1_diabetics",
    "celiac_diseases", "hypertension", "lactose_intolerance",
    "irritable_bowel_syndrome", "gout", "heart_disease", "oral_allergy_syndrome",
    "gerd",
})

CONDITION_KEYWORDS: dict[str, str] = {
    "food allergy": "food_allergy_other",
    "diabetic": "diabetics_type_2",
    "diabetes": "diabetics_type_2",
    "type 2": "diabetics_type_2",
    "type 2 diabetes": "diabetics_type_2",
    "type 1 diabetes": "type_1_diabetics",
    "type 1 diabetic": "type_1_diabetics",
    "cholesterol": "hyperlipidemia",
    "hyperlipidemia": "hyperlipidemia",
    "kidney": "kidney_disease",
    "kidney disease": "kidney_disease",
    "liver": "liver_disease",
    "liver disease": "liver_disease",
    "gluten sensitivity": "non_celiac_gluten_sensitivity",
    "non celiac": "non_celiac_gluten_sensitivity",
    "celiac": "celiac_diseases",
    "celiac disease": "celiac_diseases",
    "hypertension": "hypertension",
    "high blood pressure": "hypertension",
    "high bp": "hypertension",
    "lactose": "lactose_intolerance",
    "lactose intolerance": "lactose_intolerance",
    "ibs": "irritable_bowel_syndrome",
    "irritable bowel": "irritable_bowel_syndrome",
    "gout": "gout",
    "heart": "heart_disease",
    "heart disease": "heart_disease",
    "cardiovascular": "heart_disease",
    "oral allergy": "oral_allergy_syndrome",
    "gerd": "gerd",
    "acid reflux": "gerd",
    "reflux": "gerd",
}

CONDITION_ALIASES: dict[str, str] = {
    "diabetic": "diabetics_type_2",
    "diabetes": "diabetics_type_2",
    "type 2 diabetic": "diabetics_type_2",
    "type 1 diabetic": "type_1_diabetics",
    "hypertensive": "hypertension",
    "high blood pressure": "hypertension",
    "high bp": "hypertension",
    "cholesterol": "hyperlipidemia",
    "celiac disease": "celiac_diseases",
    "celiac diseases": "celiac_diseases",
    "celiac": "celiac_diseases",
    "kidney": "kidney_disease",
    "lactose": "lactose_intolerance",
    "lactose intolerant": "lactose_intolerance",
    "lactose intolerance": "lactose_intolerance",
    "ibs": "irritable_bowel_syndrome",
    "irritable bowel syndrome": "irritable_bowel_syndrome",
    "heart": "heart_disease",
    "heart disease": "heart_disease",
    "non celiac gluten sensitivity": "non_celiac_gluten_sensitivity",
    "ncgs": "non_celiac_gluten_sensitivity",
}

# ── Diets (DB codes / display names) ──────────────────────────────────────────

ALLOWED_DIETS: frozenset[str] = frozenset({
    "kosher", "sesame_free", "vegan", "egg_free", "renal_kidney_support",
    "carnivore", "flexitarian", "halal", "hindu_no_beef", "high_protein",
    "hyperlipidemia", "low_carb", "diabetes_friendly", "alpha_gal_syndrome",
    "oral_allergy_syndrome", "low_fat", "vegetarian_lacto_ovo", "fish_free",
    "whole_foods", "low_fodmap", "corn_free", "shellfish_free", "paleo",
    "non_celiac_gluten_sensitivity", "ketogenic", "legume_free", "mediterranean",
    "pescatarian", "dairy_free", "strict_gluten_free", "heart_healthy",
    "peanut_tree_nut_free", "soy_free", "jain_vegetarian",
})

DIET_KEYWORDS: dict[str, str] = {
    "kosher": "kosher",
    "sesame-free": "sesame_free",
    "sesame free": "sesame_free",
    "vegan": "vegan",
    "egg-free": "egg_free",
    "egg free": "egg_free",
    "renal": "renal_kidney_support",
    "kidney support": "renal_kidney_support",
    "carnivore": "carnivore",
    "flexitarian": "flexitarian",
    "halal": "halal",
    "hindu": "hindu_no_beef",
    "no beef": "hindu_no_beef",
    "high protein": "high_protein",
    "high-protein": "high_protein",
    "low carb": "low_carb",
    "low-carb": "low_carb",
    "diabetes": "diabetes_friendly",
    "diabetes friendly": "diabetes_friendly",
    "alpha gal": "alpha_gal_syndrome",
    "alpha-gal": "alpha_gal_syndrome",
    "oral allergy": "oral_allergy_syndrome",
    "low fat": "low_fat",
    "low-fat": "low_fat",
    "vegetarian": "vegetarian_lacto_ovo",
    "lacto ovo": "vegetarian_lacto_ovo",
    "lacto-ovo": "vegetarian_lacto_ovo",
    "fish-free": "fish_free",
    "fish free": "fish_free",
    "whole foods": "whole_foods",
    "fodmap": "low_fodmap",
    "low fodmap": "low_fodmap",
    "corn-free": "corn_free",
    "corn free": "corn_free",
    "shellfish-free": "shellfish_free",
    "shellfish free": "shellfish_free",
    "paleo": "paleo",
    "non celiac gluten": "non_celiac_gluten_sensitivity",
    "keto": "ketogenic",
    "ketogenic": "ketogenic",
    "legume-free": "legume_free",
    "legume free": "legume_free",
    "mediterranean": "mediterranean",
    "pescatarian": "pescatarian",
    "dairy-free": "dairy_free",
    "dairy free": "dairy_free",
    "gluten free": "strict_gluten_free",
    "gluten-free": "strict_gluten_free",
    "strict gluten": "strict_gluten_free",
    "heart healthy": "heart_healthy",
    "nut free": "peanut_tree_nut_free",
    "nut-free": "peanut_tree_nut_free",
    "peanut free": "peanut_tree_nut_free",
    "tree nut free": "peanut_tree_nut_free",
    "soy-free": "soy_free",
    "soy free": "soy_free",
    "jain": "jain_vegetarian",
}

DIET_ALIASES: dict[str, str] = {
    "veggie": "vegetarian_lacto_ovo",
    "veg": "vegetarian_lacto_ovo",
    "keto": "ketogenic",
    "vegetarian (lacto-ovo)": "vegetarian_lacto_ovo",
    "vegetarian (lacto ovo)": "vegetarian_lacto_ovo",
    "lacto-ovo vegetarian": "vegetarian_lacto_ovo",
    "peanut & tree nut free": "peanut_tree_nut_free",
    "peanut and tree nut free": "peanut_tree_nut_free",
}


def _slug(s: str) -> str:
    """Normalize to slug: lowercase, replace spaces/dashes/slashes/& with underscore."""
    s = str(s).lower().strip()
    s = re.sub(r"[\s\-/&(),]+", "_", s)
    return re.sub(r"_+", "_", s).strip("_")


def normalize_to_allergen(raw: str) -> str | None:
    """Map any user phrasing to canonical allergen code, or None if unmatchable."""
    if not raw or not str(raw).strip():
        return None
    r = str(raw).strip()
    low = r.lower()
    # Direct keyword lookup (handles "peanuts", "tree nuts", "no dairy", etc.)
    code = ALLERGEN_KEYWORDS.get(low)
    if code:
        return code
    code = ALLERGEN_KEYWORDS.get(low.rstrip("s"))  # "peanuts" -> "peanut"
    if code:
        return code
    code = ALLERGEN_ALIASES.get(low)
    if code:
        return code
    slug = _slug(r)
    code = ALLERGEN_ALIASES.get(slug)
    if code:
        return code
    if slug in ALLOWED_ALLERGENS:
        return slug
    return None


def normalize_to_condition(raw: str) -> str | None:
    """Map any user phrasing to canonical condition code, or None if unmatchable."""
    if not raw or not str(raw).strip():
        return None
    r = str(raw).strip()
    low = r.lower()
    code = CONDITION_KEYWORDS.get(low)
    if code:
        return code
    code = CONDITION_ALIASES.get(low)
    if code:
        return code
    slug = _slug(r)
    code = CONDITION_ALIASES.get(slug)
    if code:
        return code
    if slug in ALLOWED_CONDITIONS:
        return slug
    return None


def normalize_to_diet(raw: str) -> str | None:
    """Map any user phrasing to canonical diet code, or None if unmatchable."""
    if not raw or not str(raw).strip():
        return None
    r = str(raw).strip()
    low = r.lower()
    code = DIET_KEYWORDS.get(low)
    if code:
        return code
    code = DIET_KEYWORDS.get(low.replace("-", " "))
    if code:
        return code
    code = DIET_ALIASES.get(low)
    if code:
        return code
    slug = _slug(r)
    code = DIET_ALIASES.get(slug)
    if code:
        return code
    if slug in ALLOWED_DIETS:
        return slug
    return None
