"""
B2B Cypher Query Builders
=========================
Vendor-scoped Cypher queries for B2B endpoints.
Graph schema: Vendor, B2BCustomer, Product, Ingredient, Allergens,
Health_Condition, Dietary_Preferences, B2BHealthProfile, ProductCategory.

Relationships: SOLD_BY_VENDOR, BELONGS_TO_VENDOR, CONTAINS_INGREDIENT, HAS_ALLERGEN,
IS_ALLERGIC, HAS_CONDITION, FOLLOWS_DIET, HAS_PROFILE, COMPATIBLE_WITH_DIET.
"""

from __future__ import annotations

from typing import Any


def build_b2b_recommend_products(
    vendor_id: str,
    customer_id: str,
    allergen_codes: list[str],
    condition_codes: list[str],
    diet_codes: list[str],
    limit: int = 20,
    max_calories: int | None = None,
    min_protein: float | None = None,
    category_id: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """
    Recommend products for a B2B customer. Excludes products containing customer allergens.
    Uses Product→Ingredient→CONTAINS_ALLERGEN→Allergen when available;
    falls back to ingredient name matching.
    """
    params: dict[str, Any] = {
        "vendor_id": vendor_id,
        "customer_id": customer_id,
        "limit": limit,
    }
    where_parts = [
        "p.status = 'active'",
    ]
    if max_calories is not None:
        where_parts.append("(p.calories IS NULL OR p.calories <= $max_calories)")
        params["max_calories"] = max_calories
    if min_protein is not None:
        where_parts.append("(p.protein_g IS NULL OR p.protein_g >= $min_protein)")
        params["min_protein"] = min_protein
    if category_id is not None:
        where_parts.append("p.category_id = $category_id")
        params["category_id"] = category_id

    # Allergen exclusion: Product→HAS_ALLERGEN→Allergens (direct, 2,377 edges exist)
    allergen_filter = ""
    if allergen_codes:
        params["allergen_codes"] = allergen_codes
        # OLD: used 3-hop via CONTAINS_INGREDIENT→Ingredient→HAS_ALLERGEN
        # AND NOT EXISTS { MATCH (p)-[:CONTAINS_INGREDIENT]->(i:Ingredient)
        #   WHERE EXISTS { (i)-[:HAS_ALLERGEN]->(a:Allergens) WHERE a.code IN $allergen_codes } }
        # AND NOT EXISTS { MATCH (p)-[:CONTAINS_INGREDIENT]->(i:Ingredient)
        #   WHERE toLower(i.name) IN [x IN $allergen_names | toLower(x)] }
        allergen_filter = """
AND NOT EXISTS {
  MATCH (p)-[:HAS_ALLERGEN]->(a:Allergens)
  WHERE a.code IN $allergen_codes
}"""

    cypher = f"""
MATCH (c:B2BCustomer {{id: $customer_id}})-[:BELONGS_TO_VENDOR]->(v:Vendor {{id: $vendor_id}})
MATCH (p:Product)-[:SOLD_BY_VENDOR]->(v)
WHERE {' AND '.join(where_parts)}
{allergen_filter}

WITH p
ORDER BY COALESCE(p.protein_g, 0) DESC, COALESCE(p.calories, 9999) ASC
LIMIT $limit
RETURN p.id AS id, p.name AS name, p.brand AS brand,
       p.calories AS calories, p.protein_g AS protein_g,
       p.image_url AS image_url,
       0.8 AS score
"""
    return cypher, params


def build_b2b_product_customers(
    vendor_id: str,
    product_id: str,
    limit: int = 50,
    include_warnings: bool = True,
) -> tuple[str, dict[str, Any]]:
    """
    Find B2B customers who can safely consume a product.
    Excludes customers with severe/anaphylactic allergen conflicts.
    """
    params: dict[str, Any] = {
        "vendor_id": vendor_id,
        "product_id": product_id,
        "limit": limit,
    }

    # Product allergens via Ingredient→CONTAINS_ALLERGEN
    # Customer allergens with severity
    # Exclude severe/anaphylactic; include safe + optionally warning (mild)
    cypher = """
MATCH (p:Product {id: $product_id})-[:SOLD_BY_VENDOR]->(v:Vendor {id: $vendor_id})
// OLD: OPTIONAL MATCH (p)-[:CONTAINS_INGREDIENT]->(pi:Ingredient)-[:HAS_ALLERGEN]->(pa:Allergens)
// Product→HAS_ALLERGEN→Allergens already exists directly (2,377 edges)
OPTIONAL MATCH (p)-[:HAS_ALLERGEN]->(pa:Allergens)
WITH p, v, COLLECT(DISTINCT pa.code) AS product_allergen_codes

MATCH (c:B2BCustomer)-[:BELONGS_TO_VENDOR]->(v)
WHERE c.account_status = 'active' OR c.account_status IS NULL

// OLD: OPTIONAL MATCH (c)-[ca:ALLERGIC_TO]->(a:Allergens)  — actual rel is IS_ALLERGIC
OPTIONAL MATCH (c)-[ca:IS_ALLERGIC]->(a:Allergens)
WHERE a.code IN product_allergen_codes

WITH c, product_allergen_codes,
     COLLECT(DISTINCT {code: a.code, severity: ca.severity}) AS overlap_details
WITH c, product_allergen_codes, overlap_details,
     [x IN overlap_details WHERE x.code IS NOT NULL] AS overlaps
WITH c,
     CASE
       WHEN SIZE(overlaps) = 0 THEN 'safe'
       WHEN ALL(x IN overlaps WHERE coalesce(x.severity, 'mild') IN ['mild', 'moderate']) THEN 'warning'
       ELSE 'excluded'
     END AS safety_status
WHERE safety_status IN ['safe', 'warning']

OPTIONAL MATCH (c)-[:FOLLOWS_DIET]->(dp:Dietary_Preferences)
WITH c, safety_status, COLLECT(DISTINCT dp.name) AS diets
RETURN c.id AS customer_id, c.full_name AS customer_name, c.email AS email,
       safety_status,
       CASE WHEN safety_status = 'safe' THEN 1.0 ELSE 0.5 END AS match_score,
       [d IN diets WHERE d IS NOT NULL] AS diets
ORDER BY match_score DESC, c.full_name
LIMIT $limit
"""
    return cypher, params


def build_b2b_search_products(
    vendor_id: str,
    max_calories: int | None = None,
    min_protein: float | None = None,
    category: str | None = None,
    category_id: str | None = None,
    diet_codes: list[str] | None = None,
    allergen_free: list[str] | None = None,
    brand: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> tuple[str, dict[str, Any]]:
    """
    Search products by filters. Vendor-scoped.
    """
    params: dict[str, Any] = {"vendor_id": vendor_id, "limit": limit}
    where_parts: list[str] = []
    if status:
        where_parts.append("(p.status = $status OR p.status IS NULL)")
        params["status"] = status
    else:
        where_parts.append("(p.status = 'active' OR p.status IS NULL)")

    if max_calories is not None:
        where_parts.append("(p.calories IS NULL OR p.calories <= $max_calories)")
        params["max_calories"] = max_calories
    if min_protein is not None:
        where_parts.append("(p.protein_g IS NULL OR p.protein_g >= $min_protein)")
        params["min_protein"] = min_protein
    if category:
        where_parts.append("(p.category_name = $category OR toLower(p.name) CONTAINS toLower($category))")
        params["category"] = category
    if category_id:
        where_parts.append("(p.category_id = $category_id)")
        params["category_id"] = category_id
    if brand:
        where_parts.append("(toLower(p.brand) = toLower($brand) OR p.brand = $brand)")
        params["brand"] = brand
    if allergen_free:
        params["allergen_free"] = allergen_free
        # OLD: 3-hop via CONTAINS_INGREDIENT→Ingredient→HAS_ALLERGEN; direct Product→HAS_ALLERGEN exists
        # "NOT EXISTS { MATCH (p)-[:CONTAINS_INGREDIENT]->(i:Ingredient)-[:HAS_ALLERGEN]->(a:Allergens) WHERE a.code IN $allergen_free }"
        where_parts.append(
            "NOT EXISTS { MATCH (p)-[:HAS_ALLERGEN]->(a:Allergens) WHERE a.code IN $allergen_free }"
        )
    if diet_codes:
        params["diet_codes"] = diet_codes
        where_parts.append(
            "EXISTS { (p)-[:COMPATIBLE_WITH_DIET]->(dp:Dietary_Preferences) WHERE dp.code IN $diet_codes OR dp.name IN $diet_codes }"
        )

    cypher = f"""
MATCH (p:Product)-[:SOLD_BY_VENDOR]->(v:Vendor {{id: $vendor_id}})
WHERE {' AND '.join(where_parts)}
RETURN p.id AS id, p.name AS name, p.brand AS brand,
       p.calories AS calories, p.protein_g AS protein_g,
       0.9 AS score
ORDER BY COALESCE(p.protein_g, 0) DESC
LIMIT $limit
"""
    return cypher, params


def build_b2b_products_allergen_free(
    vendor_id: str,
    allergen_codes: list[str],
    limit: int = 20,
) -> tuple[str, dict[str, Any]]:
    """Products free from given allergens. Entity-driven."""
    return build_b2b_search_products(
        vendor_id=vendor_id,
        allergen_free=allergen_codes or None,
        limit=limit,
    )


def build_b2b_products_for_diet(
    vendor_id: str,
    diet_codes: list[str],
    limit: int = 20,
    max_calories: int | None = None,
    min_protein: float | None = None,
) -> tuple[str, dict[str, Any]]:
    """Products compatible with given diets. Entity-driven."""
    return build_b2b_search_products(
        vendor_id=vendor_id,
        diet_codes=diet_codes or None,
        max_calories=max_calories,
        min_protein=min_protein,
        limit=limit,
    )


# Condition codes -> diet codes (for "products for condition")
_CONDITION_TO_DIET: dict[str, list[str]] = {
    "celiac_diseases": ["strict_gluten_free", "gluten_free"],
    "non_celiac_gluten_sensitivity": ["strict_gluten_free", "gluten_free"],
    "lactose_intolerance": ["dairy_free"],
    "hypertension": ["low_carb", "heart_healthy"],
    "diabetics_type_2": ["low_carb", "diabetes_friendly"],
    "type_1_diabetics": ["low_carb", "diabetes_friendly"],
    "hyperlipidemia": ["low_fat", "heart_healthy"],
    "kidney_disease": ["renal_kidney_support"],
    "heart_disease": ["heart_healthy", "low_fat"],
    "gerd": ["low_fat"],
    "irritable_bowel_syndrome": ["low_fodmap", "strict_gluten_free"],
    "gout": ["low_fat", "low_carb"],
    "oral_allergy_syndrome": ["oral_allergy_syndrome"],
    "food_allergy_other": [],
    "liver_disease": ["low_fat"],
}


def build_b2b_products_for_condition(
    vendor_id: str,
    condition_codes: list[str],
    limit: int = 20,
) -> tuple[str, dict[str, Any]]:
    """Products suitable for customers with given conditions. Maps conditions to diets."""
    diet_codes: list[str] = []
    seen: set[str] = set()
    for c in condition_codes or []:
        for d in _CONDITION_TO_DIET.get(c, []):
            if d and d not in seen:
                seen.add(d)
                diet_codes.append(d)
    if not diet_codes:
        # Fallback: return products without diet filter (broader results)
        return build_b2b_search_products(vendor_id=vendor_id, limit=limit)
    return build_b2b_search_products(
        vendor_id=vendor_id,
        diet_codes=diet_codes,
        limit=limit,
    )


def build_b2b_customers_with_condition(
    vendor_id: str,
    condition_codes: list[str],
    limit: int = 50,
) -> tuple[str, dict[str, Any]]:
    """Find customers who have the given health conditions. Entity-driven."""
    params: dict[str, Any] = {"vendor_id": vendor_id, "limit": limit}
    if not condition_codes:
        params["condition_codes"] = []
        cypher = """
MATCH (c:B2BCustomer)-[:BELONGS_TO_VENDOR]->(v:Vendor {id: $vendor_id})
WHERE c.account_status = 'active' OR c.account_status IS NULL
RETURN c.id AS customer_id, c.full_name AS customer_name, c.email AS email
ORDER BY c.full_name
LIMIT $limit
"""
        return cypher, params

    params["condition_codes"] = condition_codes
    cypher = """
MATCH (c:B2BCustomer)-[:BELONGS_TO_VENDOR]->(v:Vendor {id: $vendor_id})
MATCH (c)-[:HAS_CONDITION]->(hc:Health_Condition)
WHERE hc.code IN $condition_codes
  AND (c.account_status = 'active' OR c.account_status IS NULL)
RETURN DISTINCT c.id AS customer_id, c.full_name AS customer_name, c.email AS email
ORDER BY c.full_name
LIMIT $limit
"""
    return cypher, params


# OLD build_b2b_substitutions (scored on calorie similarity only — gave random/irrelevant results):
# def build_b2b_substitutions(vendor_id, product_id, customer_id, limit=10):
#     ...
#     cypher = f"""
# MATCH (orig:Product {{id: $product_id}})-[:SOLD_BY_VENDOR]->(v:Vendor {{id: $vendor_id}})
# MATCH (cand:Product)-[:SOLD_BY_VENDOR]->(v)
# WHERE cand.id <> orig.id AND (cand.status = 'active' OR cand.status IS NULL)
#   AND (orig.category_id = cand.category_id OR ...)
# {allergen_filter}
# WITH orig, cand,
#      CASE WHEN orig.calories IS NOT NULL AND cand.calories IS NOT NULL
#           THEN 1.0 - abs(orig.calories - cand.calories) / COALESCE(NULLIF(orig.calories,0),1) / 100.0
#           ELSE 0.5 END AS calorie_sim
# RETURN cand.id AS id, ..., calorie_sim AS score
# ORDER BY score DESC LIMIT $limit
# """
def build_b2b_substitutions(
    vendor_id: str,
    product_id: str,
    customer_id: str | None,
    limit: int = 10,
) -> tuple[str, dict[str, Any]]:
    """
    Find substitute products scored on ingredient overlap (Jaccard) and
    nutrition similarity (calories/protein/fat). Allergen-safe when customer_id provided.
    """
    params: dict[str, Any] = {
        "vendor_id": vendor_id,
        "product_id": product_id,
        "limit": limit,
    }

    allergen_filter = ""
    if customer_id:
        params["customer_id"] = customer_id
        # OLD: used 3-hop CONTAINS_INGREDIENT→Ingredient→HAS_ALLERGEN; direct Product→HAS_ALLERGEN exists
        # AND NOT EXISTS {
        #   MATCH (c:B2BCustomer {id: $customer_id})-[:IS_ALLERGIC]->(a:Allergens)
        #   MATCH (cand)-[:CONTAINS_INGREDIENT]->(i)-[:HAS_ALLERGEN]->(a)
        # }
        allergen_filter = """
AND NOT EXISTS {
  MATCH (c:B2BCustomer {id: $customer_id})-[:IS_ALLERGIC]->(a:Allergens)
  MATCH (cand)-[:HAS_ALLERGEN]->(a)
}
"""

    # OLD used category_id (not a property in this Neo4j graph — use category_name instead):
    # AND (orig.category_id = cand.category_id OR (orig.category_id IS NULL AND cand.category_id IS NULL))
    cypher = f"""
MATCH (orig:Product {{id: $product_id}})-[:SOLD_BY_VENDOR]->(v:Vendor {{id: $vendor_id}})
MATCH (cand:Product)-[:SOLD_BY_VENDOR]->(v)
WHERE cand.id <> orig.id
  AND (cand.status = 'Active' OR cand.status = 'active' OR cand.status IS NULL)
  // OLD: case-sensitive string equality — fails if Neo4j has inconsistent casing (e.g. "Snacks" vs "snacks")
  // AND (orig.category_name = cand.category_name OR (orig.category_name IS NULL AND cand.category_name IS NULL))
  AND (toLower(orig.category_name) = toLower(cand.category_name) OR (orig.category_name IS NULL AND cand.category_name IS NULL))
{allergen_filter}

// Ingredient overlap: collect shared ingredients for Jaccard score
OPTIONAL MATCH (orig)-[:CONTAINS_INGREDIENT]->(oi:Ingredient)
WITH orig, cand, COLLECT(DISTINCT oi.id) AS orig_ings
OPTIONAL MATCH (cand)-[:CONTAINS_INGREDIENT]->(ci:Ingredient)
WITH orig, cand, orig_ings, COLLECT(DISTINCT ci.id) AS cand_ings
WITH orig, cand,
     SIZE(orig_ings) AS orig_size,
     SIZE(cand_ings) AS cand_size,
     SIZE([x IN orig_ings WHERE x IN cand_ings]) AS shared_count
WITH orig, cand, shared_count, orig_size, cand_size,
     CASE WHEN (orig_size + cand_size - shared_count) > 0
          THEN toFloat(shared_count) / toFloat(orig_size + cand_size - shared_count)
          ELSE 0.0 END AS ingredient_jaccard

// Nutrition similarity (clamped to 0..1 range)
WITH orig, cand, shared_count, ingredient_jaccard,
     CASE
       WHEN orig.calories IS NOT NULL AND cand.calories IS NOT NULL AND toFloat(orig.calories) > 0
            AND 1.0 - toFloat(abs(orig.calories - cand.calories)) / toFloat(orig.calories) > 0
       THEN 1.0 - toFloat(abs(orig.calories - cand.calories)) / toFloat(orig.calories)
       WHEN orig.calories IS NOT NULL AND cand.calories IS NOT NULL AND toFloat(orig.calories) > 0
       THEN 0.0
       ELSE 0.5 END AS calorie_sim,
     CASE
       WHEN orig.protein_g IS NOT NULL AND cand.protein_g IS NOT NULL AND toFloat(orig.protein_g) > 0
            AND 1.0 - toFloat(abs(orig.protein_g - cand.protein_g)) / toFloat(orig.protein_g) > 0
       THEN 1.0 - toFloat(abs(orig.protein_g - cand.protein_g)) / toFloat(orig.protein_g)
       WHEN orig.protein_g IS NOT NULL AND cand.protein_g IS NOT NULL AND toFloat(orig.protein_g) > 0
       THEN 0.0
       ELSE 0.5 END AS protein_sim,
     CASE
       WHEN orig.fat_g IS NOT NULL AND cand.fat_g IS NOT NULL AND toFloat(orig.fat_g) > 0
            AND 1.0 - toFloat(abs(orig.fat_g - cand.fat_g)) / toFloat(orig.fat_g) > 0
       THEN 1.0 - toFloat(abs(orig.fat_g - cand.fat_g)) / toFloat(orig.fat_g)
       WHEN orig.fat_g IS NOT NULL AND cand.fat_g IS NOT NULL AND toFloat(orig.fat_g) > 0
       THEN 0.0
       ELSE 0.5 END AS fat_sim

// Composite score: ingredient overlap 0.40 + calorie 0.30 + protein 0.20 + fat 0.10
WITH cand, shared_count, ingredient_jaccard, calorie_sim, protein_sim, fat_sim,
     (ingredient_jaccard * 0.40 + calorie_sim * 0.30 + protein_sim * 0.20 + fat_sim * 0.10) AS score

RETURN cand.id AS id, cand.name AS name, cand.brand AS brand,
       cand.calories AS calories, cand.protein_g AS protein_g, cand.fat_g AS fat_g,
       shared_count, ingredient_jaccard, calorie_sim, protein_sim,
       score
ORDER BY score DESC
LIMIT $limit
"""
    return cypher, params


def build_b2b_product_intel(
    vendor_id: str,
    product_id: str,
) -> tuple[str, dict[str, Any]]:
    """
    Diet compatibility, ingredients, and allergens for a product.
    """
    params = {"vendor_id": vendor_id, "product_id": product_id}

    cypher = """
MATCH (p:Product {id: $product_id})-[:SOLD_BY_VENDOR]->(v:Vendor {id: $vendor_id})
OPTIONAL MATCH (p)-[:COMPATIBLE_WITH_DIET]->(dp:Dietary_Preferences)
WITH p, COLLECT(DISTINCT dp.name) AS diet_names
OPTIONAL MATCH (p)-[:CONTAINS_INGREDIENT]->(i:Ingredient)
WITH p, [d IN diet_names WHERE d IS NOT NULL] AS diet_compatibility,
     [x IN COLLECT(DISTINCT i.name) WHERE x IS NOT NULL] AS ingredients
OPTIONAL MATCH (p)-[:CONTAINS_INGREDIENT]->(i2:Ingredient)-[:HAS_ALLERGEN]->(a:Allergens)
WITH p, diet_compatibility, ingredients,
     [x IN COLLECT(DISTINCT coalesce(a.name, a.code)) WHERE x IS NOT NULL] AS allergens
RETURN p.id AS product_id, diet_compatibility, ingredients, allergens
"""
    return cypher, params


def build_b2b_safety_check(
    vendor_id: str,
    product_ids: list[str] | None = None,
    customer_ids: list[str] | None = None,
) -> tuple[str, dict[str, Any]]:
    """
    Find product-customer allergen conflicts. Optional cross-reactivity via CONTAINS_ALLERGEN.
    """
    params: dict[str, Any] = {"vendor_id": vendor_id}
    where_parts: list[str] = []

    if product_ids:
        params["product_ids"] = product_ids
        where_parts.append("p.id IN $product_ids")
    if customer_ids:
        params["customer_ids"] = customer_ids
        where_parts.append("c.id IN $customer_ids")

    where_clause = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

    # OLD: used 3-hop Product→CONTAINS_INGREDIENT→Ingredient→HAS_ALLERGEN→Allergens
    # Product→HAS_ALLERGEN→Allergens already exists directly (2,377 edges) — use it instead
    cypher = f"""
MATCH (p:Product)-[:SOLD_BY_VENDOR]->(v:Vendor {{id: $vendor_id}})
MATCH (c:B2BCustomer)-[:BELONGS_TO_VENDOR]->(v)
{where_clause}
// OLD: MATCH (p)-[:CONTAINS_INGREDIENT]->(i:Ingredient)-[:HAS_ALLERGEN]->(a:Allergens)
MATCH (p)-[:HAS_ALLERGEN]->(a:Allergens)
// OLD: MATCH (c)-[ca:ALLERGIC_TO]->(a)  — ALLERGIC_TO does not exist; actual rel is IS_ALLERGIC
MATCH (c)-[ca:IS_ALLERGIC]->(a)
RETURN p.id AS product_id, p.name AS product_name,
       c.id AS customer_id, c.full_name AS customer_name,
       a.name AS conflict_allergen, a.code AS allergen_code,
       ca.severity AS customer_severity
ORDER BY CASE ca.severity
  WHEN 'anaphylactic' THEN 1
  WHEN 'severe' THEN 2
  WHEN 'moderate' THEN 3
  WHEN 'mild' THEN 4
  ELSE 5
END
"""
    return cypher, params
