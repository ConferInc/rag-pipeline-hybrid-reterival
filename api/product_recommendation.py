"""
Product recommendations for grocery list and scanner alternatives.

POST /recommend/products — Match products to ingredients (allergen-safe)
POST /recommend/alternatives — Alternative products for a scanned product

Returns empty when Product nodes or CONTAINS_INGREDIENT do not exist.
"""

from __future__ import annotations

import logging
from typing import Any

from neo4j import Driver

logger = logging.getLogger(__name__)


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
) -> set[str]:
    """Return product IDs that contain any customer allergen via Ingredient-CONTAINS_ALLERGEN->Allergens."""
    if not product_ids or not allergens:
        return set()
    allergens_clean = [a for a in allergens if a and isinstance(a, str)]
    allergens_lower = [a.lower() for a in allergens_clean]

    cypher = """
    UNWIND $product_ids AS pid
    MATCH (p:Product)
    WHERE p.id = pid OR elementId(p) = pid
    MATCH (p)-[:CONTAINS_INGREDIENT]->(i:Ingredient)-[:CONTAINS_ALLERGEN]->(a:Allergens)
    WHERE a.id IN $allergens
       OR toLower(coalesce(a.name, '')) IN $allergens_lower
       OR toLower(coalesce(a.code, '')) IN $allergens_lower
    RETURN DISTINCT coalesce(p.id, elementId(p)) AS flagged_id
    """
    try:
        with driver.session(database=database) as session:
            rows = session.run(
                cypher,
                product_ids=product_ids,
                allergens=allergens_clean,
                allergens_lower=allergens_lower,
            )
            return {str(row["flagged_id"]) for row in rows if row["flagged_id"]}
    except Exception as e:
        logger.warning("_filter_allergen_unsafe_product_ids failed: %s", e)
        return set()


def run_recommend_products(
    driver: Driver,
    *,
    ingredient_ids: list[str],
    customer_allergens: list[str] | None = None,
    database: str | None = None,
) -> dict[str, Any]:
    """
    Match products to ingredients, allergen-safe.
    Returns {products: [...]}. Empty when Product/CONTAINS_INGREDIENT not available.
    """
    products: list[dict[str, Any]] = []
    if not ingredient_ids:
        return {"products": products}

    if not _product_data_available(driver, database):
        return {"products": products}

    allergens = list(customer_allergens or [])

    # Find products that contain each ingredient
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
            rows = session.run(cypher, ingredient_ids=ingredient_ids)
            candidates: list[dict[str, Any]] = []
            seen: set[tuple[str, str]] = set()
            for row in rows:
                iid = str(row["ingredient_id"] or "")
                pid = str(row["product_id"] or "")
                if (iid, pid) in seen:
                    continue
                seen.add((iid, pid))
                candidates.append({
                    "ingredient_id": iid,
                    "product_id": pid,
                    "product_name": row["product_name"] or "",
                    "brand": row["brand"] or "",
                    "price": float(row["price"]) if row["price"] is not None else None,
                    "currency": row["currency"] or "USD",
                    "weight_g": int(row["weight_g"]) if row["weight_g"] is not None else None,
                    "category": row["category"] or "",
                    "image_url": row["image_url"] or "",
                })
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

    # Pick best product per ingredient (cheapest)
    by_ingredient: dict[str, dict] = {}
    for c in candidates:
        iid = c["ingredient_id"]
        if iid not in by_ingredient or (c.get("price") or 999999) < (by_ingredient[iid].get("price") or 999999):
            by_ingredient[iid] = c

    for c in by_ingredient.values():
        c["match_reason"] = "Allergen-safe, best price" if allergens else "Best price in category"
        products.append(c)

    return {"products": products}


def run_recommend_alternatives(
    driver: Driver,
    *,
    product_id: str,
    customer_allergens: list[str] | None = None,
    limit: int = 5,
    database: str | None = None,
) -> dict[str, Any]:
    """
    Find alternative products for a scanned product (allergen-safe, cheaper).
    Uses CAN_SUBSTITUTE first, then same-category fallback.
    Returns {alternatives: [...]}. Empty when Product data not available.
    """
    alternatives: list[dict[str, Any]] = []
    if not product_id:
        return {"alternatives": alternatives}

    if not _product_data_available(driver, database) or not _product_exists(driver, product_id, database):
        return {"alternatives": alternatives}

    allergens = list(customer_allergens or [])

    # Path A: CAN_SUBSTITUTE
    cypher_can = """
    MATCH (orig:Product)
    WHERE orig.id = $product_id OR elementId(orig) = $product_id
    MATCH (orig)-[:CAN_SUBSTITUTE]-(alt:Product)
    WHERE alt.id <> orig.id AND alt <> orig
    WITH orig, alt
    LIMIT $limit
    RETURN alt.id AS product_id, alt.name AS name, alt.brand AS brand,
           alt.price AS price, alt.image_url AS image_url, alt.category AS category,
           orig.price AS orig_price
    """
    try:
        with driver.session(database=database) as session:
            rows = session.run(cypher_can, product_id=product_id, limit=limit + 5)
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
                candidates.append({
                    "product_id": pid,
                    "name": row["name"] or "",
                    "brand": row["brand"] or "",
                    "price": float(alt_price) if alt_price is not None else None,
                    "image_url": row["image_url"] or "",
                    "category": row["category"] or "",
                    "savings": savings,
                    "reason": "Graph substitution",
                })
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
        WITH orig, alt
        ORDER BY coalesce(alt.price, 999999) ASC
        LIMIT $limit
        RETURN alt.id AS product_id, alt.name AS name, alt.brand AS brand,
               alt.price AS price, alt.image_url AS image_url, alt.category AS category,
               orig.price AS orig_price
        """
        try:
            with driver.session(database=database) as session:
                rows = session.run(cypher_cat, product_id=product_id, limit=limit + 5)
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
                    candidates.append({
                        "product_id": pid,
                        "name": row["name"] or "",
                        "brand": row["brand"] or "",
                        "price": float(alt_price) if alt_price is not None else None,
                        "image_url": row["image_url"] or "",
                        "category": row["category"] or "",
                        "savings": savings,
                        "reason": "Same category alternative",
                    })
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
