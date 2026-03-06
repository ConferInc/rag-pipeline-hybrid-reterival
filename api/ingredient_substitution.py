"""
Ingredient substitution service for POST /substitutions/ingredient.

Flow: Graph check → Semantic retrieval → Allergen filter → Diet filter
      → Nutrition enrichment → LLM fallback (if no candidates).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from neo4j import Driver

logger = logging.getLogger(__name__)


def get_ingredient_name_by_id(
    driver: Driver,
    ingredient_id: str,
    database: str | None = None,
) -> str | None:
    """Resolve ingredient name from ID (UUID or elementId)."""
    if not ingredient_id:
        return None
    cypher = """
    MATCH (i:Ingredient)
    WHERE i.id = $ingredient_id OR elementId(i) = $ingredient_id
    RETURN i.name AS name
    LIMIT 1
    """
    try:
        with driver.session(database=database) as session:
            row = session.run(cypher, ingredient_id=ingredient_id).single()
            return row["name"] if row and row["name"] else None
    except Exception as e:
        logger.warning("get_ingredient_name_by_id failed: %s", e)
        return None


def fetch_graph_substitutes(
    driver: Driver,
    ingredient_id: str,
    database: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch substitutes from CAN_SUBSTITUTE or SUBSTITUTE_FOR edges."""
    if not ingredient_id:
        return []
    cypher = """
    MATCH (orig:Ingredient)
    WHERE orig.id = $ingredient_id OR elementId(orig) = $ingredient_id
    OPTIONAL MATCH (orig)-[r:CAN_SUBSTITUTE|SUBSTITUTE_FOR]-(alt:Ingredient)
    WHERE alt IS NOT NULL
    RETURN DISTINCT alt.id AS id, alt.name AS name,
           type(r) AS rel_type, r.reason AS reason, r.confidence AS confidence
    LIMIT 10
    """
    try:
        with driver.session(database=database) as session:
            rows = session.run(cypher, ingredient_id=ingredient_id)
            out = []
            for row in rows:
                if row["id"] and row["name"]:
                    out.append({
                        "ingredient_id": str(row["id"]),
                        "name": str(row["name"]),
                        "reason": str(row["reason"]) if row["reason"] else "Graph substitution",
                        "source": "graph",
                        "score": float(row["confidence"]) if row["confidence"] is not None else 1.0,
                    })
            return out
    except Exception as e:
        logger.debug("fetch_graph_substitutes failed (expected if edges missing): %s", e)
        return []


def fetch_semantic_substitutes(
    driver: Driver,
    cfg: Any,
    embedder: Any,
    ingredient_name: str,
    ingredient_id: str,
    limit: int = 10,
    database: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch semantically similar ingredients (excluding original)."""
    from rag_pipeline.retrieval.service import retrieve_semantic, SemanticRetrievalRequest

    query = f"substitute for {ingredient_name} alternative"
    results: list[RetrievalResult] = retrieve_semantic(
        driver,
        cfg=cfg,
        embedder=embedder,
        request=SemanticRetrievalRequest(query=query, top_k=limit + 5, label="Ingredient"),
        database=database,
    )
    orig_lower = (ingredient_name or "").lower()
    out = []
    for r in results:
        payload = r.payload or {}
        rid = str(payload.get("id") or r.node_id)
        name = payload.get("name") or ""
        if not name:
            continue
        name_lower = name.lower()
        if rid == ingredient_id or name_lower == orig_lower:
            continue
        # Exclude variants (e.g. "melted butter" when original is "butter")
        if orig_lower and orig_lower in name_lower and len(name_lower) <= len(orig_lower) + 15:
            continue
        out.append({
            "ingredient_id": rid,
            "name": str(name),
            "reason": "Semantically similar ingredient",
            "source": "semantic",
            "score": float(r.score_raw),
        })
        if len(out) >= limit:
            break
    return out


def filter_allergen_violating_ingredients(
    driver: Driver,
    ingredient_ids: list[str],
    allergens: list[str],
    database: str | None = None,
) -> set[str]:
    """Return set of ingredient IDs that contain any customer allergen."""
    if not ingredient_ids or not allergens:
        return set()
    allergens_lower = [a.lower() for a in allergens if a and isinstance(a, str)]
    if not allergens_lower:
        return set()

    # Try graph: (Ingredient)-[:CONTAINS_ALLERGEN]->(Allergen/Allergens)
    # Support both allergen IDs and names
    cypher_graph = """
    UNWIND $ingredient_ids AS iid
    MATCH (i:Ingredient)
    WHERE (i.id = iid OR elementId(i) = iid)
    MATCH (i)-[:CONTAINS_ALLERGEN]->(a)
    WHERE a.id IN $allergens
       OR toLower(coalesce(a.name, '')) IN $allergens_lower
       OR toLower(coalesce(a.code, '')) IN $allergens_lower
    RETURN DISTINCT coalesce(i.id, elementId(i)) AS flagged_id
    """
    try:
        with driver.session(database=database) as session:
            rows = session.run(
                cypher_graph,
                ingredient_ids=ingredient_ids,
                allergens=allergens,
                allergens_lower=allergens_lower,
            )
            flagged = {str(row["flagged_id"]) for row in rows if row["flagged_id"]}
            if flagged:
                return flagged
    except Exception:
        pass

    # Fallback: name CONTAINS allergen term
    cypher_name = """
    UNWIND $ingredient_ids AS iid
    MATCH (i:Ingredient)
    WHERE (i.id = iid OR elementId(i) = iid)
      AND ANY(a IN $allergens WHERE toLower(i.name) CONTAINS a)
    RETURN DISTINCT i.id AS flagged_id
    """
    try:
        with driver.session(database=database) as session:
            rows = session.run(
                cypher_name,
                ingredient_ids=ingredient_ids,
                allergens=allergens_lower,
            )
            return {str(row["flagged_id"]) for row in rows if row["flagged_id"]}
    except Exception as e:
        logger.warning("filter_allergen_violating_ingredients failed: %s", e)
        return set()


def filter_diet_violating_ingredients(
    driver: Driver,
    ingredient_ids: list[str],
    diets: list[str],
    database: str | None = None,
) -> set[str]:
    """Return set of ingredient IDs forbidden by customer's diets (FORBIDS)."""
    if not ingredient_ids or not diets:
        return set()
    diets_clean = [d for d in diets if d and isinstance(d, str)]
    if not diets_clean:
        return set()

    cypher = """
    UNWIND $ingredient_ids AS iid
    MATCH (i:Ingredient)
    WHERE i.id = iid OR elementId(i) = iid
    MATCH (dp:Dietary_Preferences)-[:FORBIDS]->(i)
    WHERE dp.name IN $diets
    RETURN DISTINCT i.id AS flagged_id
    """
    try:
        with driver.session(database=database) as session:
            rows = session.run(cypher, ingredient_ids=ingredient_ids, diets=diets_clean)
            return {str(row["flagged_id"]) for row in rows if row["flagged_id"]}
    except Exception as e:
        logger.warning("filter_diet_violating_ingredients failed: %s", e)
        return set()


def fetch_nutrition_for_ingredients(
    driver: Driver,
    ingredient_ids: list[str],
    database: str | None = None,
) -> dict[str, dict[str, float]]:
    """Fetch calories, protein_g, total_fat_g for ingredients (inline props or HAS_NUTRITION)."""
    if not ingredient_ids:
        return {}

    cypher = """
    UNWIND $ingredient_ids AS iid
    MATCH (i:Ingredient)
    WHERE i.id = iid OR elementId(i) = iid
    RETURN coalesce(i.id, elementId(i)) AS id, i.name AS name,
           i.calories AS calories, i.protein_g AS protein_g, i.total_fat_g AS total_fat_g
    """
    result: dict[str, dict[str, float]] = {}
    try:
        with driver.session(database=database) as session:
            rows = session.run(cypher, ingredient_ids=ingredient_ids)
            for row in rows:
                rid = str(row["id"]) if row["id"] else ""
                if not rid:
                    continue
                cal = row["calories"]
                prot = row["protein_g"]
                fat = row["total_fat_g"]
                if cal is not None or prot is not None or fat is not None:
                    result[rid] = {
                        "calories_per_100g": float(cal) if cal is not None else None,
                        "protein_g": float(prot) if prot is not None else None,
                        "total_fat_g": float(fat) if fat is not None else None,
                    }
    except Exception as e:
        logger.warning("fetch_nutrition_for_ingredients failed: %s", e)
    return result


def llm_substitution_fallback(
    ingredient_name: str,
    allergens: list[str],
    diets: list[str],
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Use LLM to suggest substitutes when graph/semantic return nothing."""
    from rag_pipeline.generation.generator import generate_response

    allergen_str = ", ".join(allergens[:5]) if allergens else "none specified"
    diet_str = ", ".join(diets[:3]) if diets else "none specified"
    prompt = f"""You are a food substitution assistant. Suggest up to {limit} substitute ingredients for: {ingredient_name}.
Consider: customer allergens ({allergen_str}), diets ({diet_str}). Exclude substitutes that contain customer allergens.
Return a JSON array only, no markdown, e.g.:
[{{"name": "Coconut Oil", "reason": "Dairy-free fat substitute"}}]
"""
    try:
        resp = generate_response(prompt, temperature=0.3, max_tokens=512)
        # Strip possible markdown
        text = (resp or "").strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        text = text.strip()
        arr = json.loads(text)
        if not isinstance(arr, list):
            return []
        out = []
        for i, item in enumerate(arr[:limit]):
            if isinstance(item, dict) and item.get("name"):
                out.append({
                    "ingredient_id": "",
                    "name": str(item["name"]),
                    "reason": str(item.get("reason", "LLM suggestion")),
                    "source": "llm",
                    "score": 0.9 - (i * 0.1),
                })
        return out
    except Exception as e:
        logger.warning("llm_substitution_fallback failed: %s", e)
        return []


def run_ingredient_substitution(
    driver: Driver,
    cfg: Any,
    embedder: Any,
    *,
    ingredient_id: str,
    ingredient_name: str | None = None,
    customer_allergens: list[str] | None = None,
    customer_diets: list[str] | None = None,
    limit: int = 5,
    database: str | None = None,
    debug: bool = False,
) -> dict[str, Any]:
    """
    Main substitution flow. Returns {substitutions, debug_info?}.
    """
    allergens = list(customer_allergens or [])
    diets = list(customer_diets or [])
    debug_info: dict[str, Any] = {} if debug else None

    name = ingredient_name or get_ingredient_name_by_id(driver, ingredient_id, database)
    if not name:
        name = "unknown"

    # 1. Graph check
    graph_subs = fetch_graph_substitutes(driver, ingredient_id, database)
    if debug_info is not None:
        debug_info["graph"] = {"count": len(graph_subs), "used": len(graph_subs) > 0}

    candidates = graph_subs if graph_subs else []

    # 2. Semantic retrieval if no graph results
    if not candidates and cfg and embedder:
        semantic_subs = fetch_semantic_substitutes(
            driver, cfg, embedder, name, ingredient_id, limit=limit + 5, database=database
        )
        candidates = semantic_subs
        if debug_info is not None:
            debug_info["semantic"] = {"count": len(semantic_subs), "used": True}

    if debug_info is not None and "semantic" not in debug_info:
        debug_info["semantic"] = {"count": 0, "used": False}

    # 3. Allergen filter
    if candidates and allergens:
        ids = [c["ingredient_id"] for c in candidates if c.get("ingredient_id")]
        if ids:
            violating = filter_allergen_violating_ingredients(driver, ids, allergens, database)
            candidates = [c for c in candidates if c.get("ingredient_id") not in violating]
            if debug_info is not None:
                debug_info["allergen_filter"] = {"violating_count": len(violating)}

    # 4. Diet filter
    if candidates and diets:
        ids = [c["ingredient_id"] for c in candidates if c.get("ingredient_id")]
        if ids:
            violating = filter_diet_violating_ingredients(driver, ids, diets, database)
            candidates = [c for c in candidates if c.get("ingredient_id") not in violating]
            if debug_info is not None:
                debug_info["diet_filter"] = {"violating_count": len(violating)}

    # 5. LLM fallback if still no candidates
    if not candidates:
        llm_subs = llm_substitution_fallback(name, allergens, diets, limit=limit)
        candidates = llm_subs
        if debug_info is not None:
            debug_info["llm_fallback"] = {"used": True, "count": len(llm_subs)}

    if debug_info is not None and "llm_fallback" not in debug_info:
        debug_info["llm_fallback"] = {"used": False}

    # 6. Nutrition enrichment
    all_ids = [c["ingredient_id"] for c in candidates if c.get("ingredient_id")]
    if ingredient_id:
        all_ids = [ingredient_id] + [i for i in all_ids if i != ingredient_id]
    nutrition_map = fetch_nutrition_for_ingredients(driver, list(dict.fromkeys(all_ids)), database)
    orig_nut = nutrition_map.get(ingredient_id) if ingredient_id else None

    # 7. Build final response
    substitutions = []
    for c in candidates[:limit]:
        sub_id = c.get("ingredient_id", "")
        sub_nut = nutrition_map.get(sub_id) if sub_id else None
        nc = None
        if orig_nut or sub_nut:
            nc = {
                "original": {k: v for k, v in (orig_nut or {}).items() if v is not None},
                "substitute": {k: v for k, v in (sub_nut or {}).items() if v is not None},
            }
            if not nc["original"] and nc["substitute"]:
                nc["original"] = {}
            if not nc["substitute"] and nc["original"]:
                nc["substitute"] = {}

        substitutions.append({
            "ingredient_id": sub_id,
            "name": c.get("name", ""),
            "reason": c.get("reason", ""),
            "source": c.get("source", "unknown"),
            "nutritionComparison": nc,
            "allergenSafe": bool(candidates),
        })

    result: dict[str, Any] = {"substitutions": substitutions}
    if debug_info is not None:
        result["debug_info"] = debug_info
    return result
