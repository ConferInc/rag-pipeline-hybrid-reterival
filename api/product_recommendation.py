"""
Product recommendations for grocery list and scanner alternatives.

POST /recommend/products — Match products to ingredients (allergen-safe)
  Supports quality_preferences (certification hard filter) and preferred_brands (soft boost).
POST /recommend/alternatives — Alternative products for a scanned product

Returns empty when Product nodes or CONTAINS_INGREDIENT do not exist.
"""

from __future__ import annotations

import logging
from typing import Any

from neo4j import Driver

logger = logging.getLogger(__name__)

# PRD-30: Map household preference types to Neo4j Certification codes
QUALITY_TO_CERTIFICATION: dict[str, str] = {
    "organic": "USDA_ORGANIC",
    "non_gmo": "NON_GMO_PROJECT",
    "halal": "HALAL",
    "kosher": "KOSHER",
    "no_msg": "NO_MSG",
    "grass_fed": "GRASS_FED",
    "hormone_free": "HORMONE_FREE",
    "pesticide_free": "PESTICIDE_FREE",
}

BRAND_BOOST = 0.2


def get_match_confidence(
    *,
    ingredient_id_returned: str,
    ingredient_name_returned: str,
    iid: str,
    iname: str,
) -> str:
    """
    Classify how well a graph ingredient node matched the caller's search term.

    Returns one of:
      "id_match"     — graph node ID equals the search ID (exact database lookup)
      "name_exact"   — graph node name exactly equals the search name (case-insensitive)
      "name_partial" — one name contains the other (case-insensitive substring)
      "name_unknown" — no reliable match signal found
    """
    if ingredient_id_returned and iid and ingredient_id_returned == iid:
        return "id_match"
    a = ingredient_name_returned.lower().strip()
    b = iname.lower().strip()
    if a and b and a == b:
        return "name_exact"
    if a and b and (a in b or b in a):
        return "name_partial"
    return "name_unknown"


def _product_data_available(driver: Driver, database: str | None = None) -> bool:
    """Check if Product nodes exist (CONTAINS_INGREDIENT checked per-query)."""
    cypher = "MATCH (p:Product) RETURN 1 AS x LIMIT 1"
    try:
        with driver.session(database=database) as session:
            row = session.run(cypher).single()
            return row is not None
    except Exception as e:
        logger.debug("_product_data_available check failed: %s", e)
        return False


def _product_exists(driver: Driver, product_id: str, database: str | None = None) -> bool:
    """Check if a Product with given id exists."""
    if not product_id:
        return False
    cypher = "MATCH (p:Product) WHERE p.id = $product_id OR elementId(p) = $product_id RETURN 1 AS x LIMIT 1"
    try:
        with driver.session(database=database) as session:
            row = session.run(cypher, product_id=product_id).single()
            return row is not None
    except Exception as e:
        logger.debug("_product_exists check failed: %s", e)
        return False


def _filter_allergen_unsafe_product_ids(
    driver: Driver,
    product_ids: list[str],
    allergens: list[str],
    database: str | None = None,
) -> dict[str, dict[str, list[str]]]:
    """Return a map of unsafe products → matched allergen detail.

    For every product that contains any of the supplied customer allergens (via
    Product-CONTAINS_INGREDIENT->Ingredient-CONTAINS_ALLERGEN->Allergens), the
    map holds::

        { product_id: {"matching_allergens": [name, ...], "allergen_codes": [code, ...]} }

    Backward-compatible with the previous ``set[str]`` return: callers that test
    ``pid in result`` / ``pid not in result`` are unaffected because membership
    tests against a dict check its keys. Callers that need the matched-allergen
    detail (PRD-40 safety annotation) read the values. Returns ``{}`` on error or
    when there is nothing to check.
    """
    if not product_ids or not allergens:
        return {}
    allergens_clean = [a for a in allergens if a and isinstance(a, str)]
    allergens_lower = [a.lower() for a in allergens_clean]

    # NOTE: the live graph uses Ingredient-[:HAS_ALLERGEN]->Allergens (verified via
    # neo4j_schema_dump.json); historical code/docs used CONTAINS_ALLERGEN. We match
    # BOTH via relationship alternation so safety works regardless of which exists.
    cypher = """
    UNWIND $product_ids AS pid
    MATCH (p:Product)
    WHERE p.id = pid OR elementId(p) = pid
    MATCH (p)-[:CONTAINS_INGREDIENT]->(i:Ingredient)-[:HAS_ALLERGEN|CONTAINS_ALLERGEN]->(a:Allergens)
    WHERE a.id IN $allergens
       OR toLower(coalesce(a.name, '')) IN $allergens_lower
       OR toLower(coalesce(a.code, '')) IN $allergens_lower
    RETURN coalesce(p.id, elementId(p)) AS flagged_id,
           collect(DISTINCT a.name) AS matching_allergens,
           collect(DISTINCT a.code) AS allergen_codes
    """
    try:
        with driver.session(database=database) as session:
            rows = session.run(
                cypher,
                product_ids=product_ids,
                allergens=allergens_clean,
                allergens_lower=allergens_lower,
            )
            result: dict[str, dict[str, list[str]]] = {}
            for row in rows:
                fid = row["flagged_id"]
                if not fid:
                    continue
                result[str(fid)] = {
                    "matching_allergens": [x for x in (row["matching_allergens"] or []) if x],
                    "allergen_codes": [x for x in (row["allergen_codes"] or []) if x],
                }
            return result
    except Exception as e:
        logger.warning("_filter_allergen_unsafe_product_ids failed: %s", e)
        return {}


def run_explain_allergens(
    driver: Driver,
    *,
    product_id: str,
    allergen_codes: list[str] | None = None,
    database: str | None = None,
) -> dict[str, Any]:
    """Explain how a product's allergens are detected via the ingredient graph.

    Returns ``{ allergens: [{ code, allergen, ingredients: [names] }] }``. When
    ``allergen_codes`` is empty, returns ALL allergens the product carries.
    Matches both HAS_ALLERGEN (live graph) and CONTAINS_ALLERGEN (legacy).
    """
    if not product_id:
        return {"allergens": []}
    codes = [c for c in (allergen_codes or []) if c and isinstance(c, str)]
    codes_lower = [c.lower() for c in codes]
    cypher = """
    MATCH (p:Product)
    WHERE p.id = $product_id OR elementId(p) = $product_id
    MATCH (p)-[:CONTAINS_INGREDIENT]->(i:Ingredient)-[:HAS_ALLERGEN|CONTAINS_ALLERGEN]->(a:Allergens)
    WHERE size($codes) = 0
       OR a.id IN $codes
       OR toLower(coalesce(a.name, '')) IN $codes_lower
       OR toLower(coalesce(a.code, '')) IN $codes_lower
    RETURN coalesce(a.code, a.name) AS code, a.name AS allergen,
           collect(DISTINCT i.name) AS ingredients
    ORDER BY allergen
    """
    try:
        with driver.session(database=database) as session:
            rows = session.run(cypher, product_id=product_id, codes=codes, codes_lower=codes_lower)
            out: list[dict[str, Any]] = []
            for r in rows:
                out.append({
                    "code": r["code"],
                    "allergen": r["allergen"],
                    "ingredients": [x for x in (r["ingredients"] or []) if x],
                })
            return {"allergens": out}
    except Exception as e:
        logger.warning("run_explain_allergens failed: %s", e)
        return {"allergens": []}


def _map_quality_to_cert_codes(quality_preferences: list[str] | None) -> list[str]:
    """Map quality preference strings to Neo4j Certification codes. Skips unknown types."""
    if not quality_preferences:
        return []
    codes: list[str] = []
    for q in quality_preferences:
        if q and isinstance(q, str):
            key = q.lower().strip()
            if key in QUALITY_TO_CERTIFICATION:
                codes.append(QUALITY_TO_CERTIFICATION[key])
            else:
                logger.debug("Unknown quality preference '%s', skipping", q)
    return codes


def _filter_products_by_certification(
    driver: Driver,
    product_ids: list[str],
    quality_codes: list[str],
    database: str | None = None,
) -> set[str]:
    """
    Return product IDs that have ALL requested certifications (AND logic).
    Uses Product-[:HAS_CERTIFICATION]->Certification. Returns empty on error.
    """
    if not product_ids or not quality_codes:
        return set()
    try:
        cypher = """
        UNWIND $product_ids AS pid
        MATCH (p:Product)
        WHERE p.id = pid OR elementId(p) = pid
        OPTIONAL MATCH (p)-[:HAS_CERTIFICATION]->(c:Certification)
        WHERE c.code IN $quality_codes
        WITH p, [x IN collect(DISTINCT c.code) WHERE x IS NOT NULL] AS certs
        WHERE ALL(code IN $quality_codes WHERE code IN certs)
        RETURN coalesce(p.id, elementId(p)) AS product_id
        """
        with driver.session(database=database) as session:
            rows = session.run(
                cypher,
                product_ids=product_ids,
                quality_codes=quality_codes,
            )
            return {str(row["product_id"]) for row in rows if row["product_id"]}
    except Exception as e:
        logger.warning("_filter_products_by_certification failed: %s", e)
        return set()


def run_recommend_products(
    driver: Driver,
    *,
    ingredient_ids: list[str],
    ingredient_names: dict[str, str] | None = None,
    customer_allergens: list[str] | None = None,
    quality_preferences: list[str] | None = None,
    preferred_brands: list[str] | None = None,
    household_budget: float | None = None,
    database: str | None = None,
) -> dict[str, Any]:
    """
    Match products to ingredients, allergen-safe.
    Supports quality_preferences (certification hard filter, two-phase) and
    preferred_brands (soft boost). Fallback to best available when no certified products.
    Returns {products: [...]}. Empty when Product/CONTAINS_INGREDIENT not available.
    """
    products: list[dict[str, Any]] = []
    if not ingredient_ids:
        return {"products": products}

    if not _product_data_available(driver, database):
        return {"products": products}

    allergens = list(customer_allergens or [])
    quality_codes = _map_quality_to_cert_codes(quality_preferences)
    brands = [b for b in (preferred_brands or []) if b and isinstance(b, str)]
    brands_lower = [b.lower().strip() for b in brands]
    has_quality_prefs = bool(quality_codes)
    has_brand_prefs = bool(brands_lower)

    # Phase 1: Find products that contain each ingredient
    # Build name list for fallback matching when IDs don't match across systems
    name_map = ingredient_names or {}
    name_list = [name_map.get(iid, "").lower().strip() for iid in ingredient_ids]
    has_names = any(n for n in name_list)

    if has_names:
        # Match by ID first, then fall back to exact name match
        cypher = """
        UNWIND range(0, size($ingredient_ids)-1) AS idx
        WITH $ingredient_ids[idx] AS iid, $ingredient_names[idx] AS iname
        MATCH (i:Ingredient)<-[:CONTAINS_INGREDIENT]-(p:Product)
        WHERE i.id = iid OR elementId(i) = iid OR (iname <> '' AND toLower(i.name) = iname)
        WITH i, p, iid
        ORDER BY coalesce(p.price, 999999) ASC
        RETURN iid AS ingredient_id, i.name AS ingredient_name,
               p.id AS product_id, p.name AS product_name, p.brand AS brand,
               p.price AS price, p.currency AS currency,
               p.weight_g AS weight_g, p.category AS category, p.image_url AS image_url
        """
    else:
        cypher = """
        UNWIND $ingredient_ids AS iid
        MATCH (i:Ingredient)<-[:CONTAINS_INGREDIENT]-(p:Product)
        WHERE i.id = iid OR elementId(i) = iid
        WITH i, p
        ORDER BY coalesce(p.price, 999999) ASC
        RETURN i.id AS ingredient_id, i.name AS ingredient_name,
               p.id AS product_id, p.name AS product_name, p.brand AS brand,
               p.price AS price, p.currency AS currency,
               p.weight_g AS weight_g, p.category AS category, p.image_url AS image_url
        """
    try:
        with driver.session(database=database) as session:
            params: dict[str, Any] = {"ingredient_ids": ingredient_ids}
            if has_names:
                params["ingredient_names"] = name_list
            rows = session.run(cypher, **params)
            candidates: list[dict[str, Any]] = []
            seen: set[tuple[str, str]] = set()
            for row in rows:
                iid = str(row["ingredient_id"] or "")
                pid = str(row["product_id"] or "")
                if (iid, pid) in seen:
                    continue
                seen.add((iid, pid))
                # graph_ingredient_id is the graph node's own ID (may differ from iid
                # when the match was found by name rather than by our stored UUID)
                _graph_iid_raw = row.get("graph_ingredient_id") if hasattr(row, "get") else None
                graph_iid = str(_graph_iid_raw or row["ingredient_id"] or "")
                iname = name_map.get(iid, "")
                ingredient_name_returned = str(row.get("ingredient_name") or "" if hasattr(row, "get") else "")
                confidence = get_match_confidence(
                    ingredient_id_returned=graph_iid,
                    ingredient_name_returned=ingredient_name_returned,
                    iid=iid,
                    iname=iname,
                )
                candidates.append(
                    {
                        "ingredient_id": iid,
                        "product_id": pid,
                        "product_name": row["product_name"] or "",
                        "brand": row["brand"] or "",
                        "price": float(row["price"]) if row["price"] is not None else None,
                        "currency": row["currency"] or "USD",
                        "weight_g": int(row["weight_g"]) if row["weight_g"] is not None else None,
                        "category": row["category"] or "",
                        "image_url": row["image_url"] or "",
                        "match_confidence": confidence,
                    }
                )
    except Exception as e:
        logger.warning("run_recommend_products query failed: %s", e)
        return {"products": []}

    if not candidates:
        return {"products": []}

    # Filter by allergens
    if allergens:
        product_ids = list({c["product_id"] for c in candidates})
        unsafe = _filter_allergen_unsafe_product_ids(driver, product_ids, allergens, database)
        candidates = [c for c in candidates if c["product_id"] not in unsafe]

    if not candidates:
        return {"products": []}

    # Filter by household budget when provided (exclude products above budget)
    if household_budget is not None and household_budget > 0:
        candidates = [c for c in candidates if (c.get("price") or 0) <= household_budget]

    if not candidates:
        return {"products": []}

    preference_matched = True

    # Phase 2: Certification filter (hard constraint, two-phase)
    if has_quality_prefs:
        product_ids = list({c["product_id"] for c in candidates})
        certified_ids = _filter_products_by_certification(driver, product_ids, quality_codes, database)
        certified_candidates = [c for c in candidates if c["product_id"] in certified_ids]
        if certified_candidates:
            candidates = certified_candidates
        else:
            # Fallback: no certified products, use best available (GP-7)
            preference_matched = False
            logger.debug(
                "No products with certifications %s; returning best available",
                quality_codes,
            )

    # Apply brand boost (soft) and select best per ingredient
    for c in candidates:
        brand_val = (c.get("brand") or "").strip()
        is_preferred_brand = brand_val.lower() in brands_lower if brands_lower else False
        base_price = c.get("price") or 999999.0
        score = 1.0 / (float(base_price) + 0.01)
        if is_preferred_brand:
            score += BRAND_BOOST
        c["_score"] = score

    by_ingredient: dict[str, dict] = {}
    for c in candidates:
        iid = c["ingredient_id"]
        if iid not in by_ingredient or c["_score"] > by_ingredient[iid]["_score"]:
            by_ingredient[iid] = c

    for c in by_ingredient.values():
        del c["_score"]
        if preference_matched:
            if has_quality_prefs and has_brand_prefs:
                c["match_reason"] = "Matches quality preferences, best price"
            elif has_quality_prefs:
                c["match_reason"] = "Matches quality preferences, best price"
            elif has_brand_prefs:
                c["match_reason"] = "Preferred brand, best price"
            else:
                c["match_reason"] = "Allergen-safe, best price" if allergens else "Best price in category"
        else:
            c["match_reason"] = "Best available; no certified products found"
        c["preference_matched"] = preference_matched
        products.append(c)

    return {"products": products}


def run_recommend_alternatives(
    driver: Driver,
    *,
    product_id: str,
    customer_allergens: list[str] | None = None,
    limit: int = 5,
    exclude_ids: list[str] | None = None,
    database: str | None = None,
) -> dict[str, Any]:
    """
    Find alternative products for a scanned product (allergen-safe, cheaper).
    Uses CAN_SUBSTITUTE first, then same-category fallback.
    `exclude_ids` drops specific products (e.g. the original + already-substituted
    items in an OOS recovery flow — PRD-40.1 E13). Backward-compatible: defaults
    to no exclusions. Returns {alternatives: [...]}. Empty when Product data
    not available.
    """
    alternatives: list[dict[str, Any]] = []
    if not product_id:
        return {"alternatives": alternatives}

    if not _product_data_available(driver, database) or not _product_exists(driver, product_id, database):
        return {"alternatives": alternatives}

    allergens = list(customer_allergens or [])
    exclude = [str(x) for x in (exclude_ids or []) if x]

    # Path A: CAN_SUBSTITUTE
    cypher_can = """
    MATCH (orig:Product)
    WHERE orig.id = $product_id OR elementId(orig) = $product_id
    MATCH (orig)-[:CAN_SUBSTITUTE]-(alt:Product)
    WHERE alt.id <> orig.id AND alt <> orig
      AND NOT coalesce(alt.id, elementId(alt)) IN $exclude_ids
    WITH orig, alt
    LIMIT $limit
    RETURN alt.id AS product_id, alt.name AS name, alt.brand AS brand,
           alt.price AS price, alt.image_url AS image_url, alt.category AS category,
           orig.price AS orig_price
    """
    try:
        with driver.session(database=database) as session:
            rows = session.run(cypher_can, product_id=product_id, limit=limit + 5, exclude_ids=exclude)
            candidates = []
            for row in rows:
                pid = str(row["product_id"] or "")
                orig_price = row["orig_price"]
                alt_price = row["price"]
                savings = None
                if orig_price is not None and alt_price is not None:
                    try:
                        savings = float(orig_price) - float(alt_price)
                        if savings < 0:
                            savings = None
                    except (TypeError, ValueError):
                        pass
                candidates.append(
                    {
                        "product_id": pid,
                        "name": row["name"] or "",
                        "brand": row["brand"] or "",
                        "price": float(alt_price) if alt_price is not None else None,
                        "image_url": row["image_url"] or "",
                        "category": row["category"] or "",
                        "savings": savings,
                        "reason": "Graph substitution",
                    }
                )
    except Exception:
        candidates = []

    # Path B: Same category fallback if CAN_SUBSTITUTE returned nothing
    if not candidates:
        cypher_cat = """
        MATCH (orig:Product)
        WHERE orig.id = $product_id OR elementId(orig) = $product_id
        WITH orig
        MATCH (alt:Product)
        WHERE alt <> orig
          AND (orig.category IS NOT NULL AND alt.category = orig.category)
          AND NOT coalesce(alt.id, elementId(alt)) IN $exclude_ids
        WITH orig, alt
        ORDER BY coalesce(alt.price, 999999) ASC
        LIMIT $limit
        RETURN alt.id AS product_id, alt.name AS name, alt.brand AS brand,
               alt.price AS price, alt.image_url AS image_url, alt.category AS category,
               orig.price AS orig_price
        """
        try:
            with driver.session(database=database) as session:
                rows = session.run(cypher_cat, product_id=product_id, limit=limit + 5, exclude_ids=exclude)
                for row in rows:
                    pid = str(row["product_id"] or "")
                    orig_price = row["orig_price"]
                    alt_price = row["price"]
                    savings = None
                    if orig_price is not None and alt_price is not None:
                        try:
                            savings = float(orig_price) - float(alt_price)
                            if savings < 0:
                                savings = None
                        except (TypeError, ValueError):
                            pass
                    candidates.append(
                        {
                            "product_id": pid,
                            "name": row["name"] or "",
                            "brand": row["brand"] or "",
                            "price": float(alt_price) if alt_price is not None else None,
                            "image_url": row["image_url"] or "",
                            "category": row["category"] or "",
                            "savings": savings,
                            "reason": "Same category alternative",
                        }
                    )
        except Exception as e:
            logger.debug("run_recommend_alternatives category fallback failed: %s", e)

    if not candidates:
        return {"alternatives": []}

    # Filter by allergens
    if allergens:
        product_ids = [c["product_id"] for c in candidates]
        unsafe = _filter_allergen_unsafe_product_ids(driver, product_ids, allergens, database)
        candidates = [c for c in candidates if c["product_id"] not in unsafe]

    for c in candidates[:limit]:
        c["allergen_safe"] = True
        alternatives.append(c)

    return {"alternatives": alternatives}
