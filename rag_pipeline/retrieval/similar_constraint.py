"""
Cypher-based retrieval: recipes from users with overlapping dietary constraints.

Used when profile is aggregated (family/couple scope). Replaces structural retrieval
(which seeds from a single B2C_Customer) with "users who share similar diets/allergens
and what they saved/viewed."

Returns same format as structural_search_with_expansion for RRF fusion compatibility.
"""

from __future__ import annotations

import logging
from typing import Any

from neo4j import Driver

logger = logging.getLogger(__name__)


def retrieve_recipes_from_similar_constraint_users(
    driver: Driver,
    *,
    diets: list[str] | None = None,
    allergens: list[str] | None = None,
    health_conditions: list[str] | None = None,
    top_k: int = 20,
    database: str | None = None,
) -> dict[str, Any]:
    """
    Find recipes from B2C_Customers whose constraints overlap with the given profile.

    Overlap: customer follows at least one of our diets, or is allergic to at least
    one of our allergens. Expands to their SAVED/VIEWED recipes. Hard constraints
    applied later will filter to our full profile.

    Returns dict with same shape as structural_search_with_expansion:
      {"similar_nodes": [], "expanded_context": [...]}
    for RRF fusion compatibility.
    """
    diets_clean = [d for d in (diets or []) if d and isinstance(d, str)]
    allergens_clean = [a for a in (allergens or []) if a and isinstance(a, str)]
    conditions_clean = [c for c in (health_conditions or []) if c and isinstance(c, str)]

    if not diets_clean and not allergens_clean and not conditions_clean:
        logger.debug(
            "retrieve_recipes_from_similar_constraint_users: empty profile, skipping",
        )
        return {"similar_nodes": [], "expanded_context": []}

    # Build UNION subqueries for each constraint type (diet, allergen, condition overlap)
    union_parts: list[str] = []
    params: dict[str, Any] = {"top_k": int(top_k)}

    if diets_clean:
        params["diets"] = diets_clean
        union_parts.append("""
            MATCH (c:B2C_Customer)-[:FOLLOWS_DIET]->(dp:Dietary_Preferences)
            WHERE dp.name IN $diets
            RETURN c
        """)
    if allergens_clean:
        params["allergens"] = allergens_clean
        union_parts.append("""
            MATCH (c:B2C_Customer)-[:IS_ALLERGIC]->(a:Allergens)
            WHERE a.name IN $allergens
            RETURN c
        """)
    if conditions_clean:
        params["conditions"] = conditions_clean
        union_parts.append("""
            MATCH (c:B2C_Customer)-[:HAS_CONDITION]->(hc:B2C_Customer_Health_Conditions)
            WHERE hc.name IN $conditions
            RETURN c
        """)

    if not union_parts:
        return {"similar_nodes": [], "expanded_context": []}

    if len(union_parts) == 1:
        customer_match = union_parts[0].strip().replace("RETURN c", "")
        cypher = f"""
        {customer_match}
        WITH DISTINCT c
        MATCH (c)-[:SAVED|VIEWED]->(r_node:Recipe)
        WITH r_node, count(*) AS engagement
        ORDER BY engagement DESC
        WITH collect(DISTINCT r_node)[0..$top_k] AS recipe_nodes
        UNWIND recipe_nodes AS r_node
        RETURN r_node
        """
    else:
        union_clause = " UNION ".join(p.strip() for p in union_parts)
        cypher = f"""
        CALL {{
            {union_clause}
        }}
        WITH DISTINCT c
        MATCH (c)-[:SAVED|VIEWED]->(r_node:Recipe)
        WITH r_node, count(*) AS engagement
        ORDER BY engagement DESC
        WITH collect(DISTINCT r_node)[0..$top_k] AS recipe_nodes
        UNWIND recipe_nodes AS r_node
        RETURN r_node
        """
    try:
        with driver.session(database=database) as session:
            result = session.run(cypher, **params)
            expanded: list[dict[str, Any]] = []
            seen_ids: set[str] = set()
            for record in result:
                r_node = record.get("r_node")
                if not r_node:
                    continue
                node_dict = dict(r_node)
                node_id = str(node_dict.get("id", "") or getattr(r_node, "element_id", id(r_node)))
                if node_id in seen_ids:
                    continue
                seen_ids.add(node_id)
                payload = _build_recipe_payload(node_dict)
                payload["id"] = node_dict.get("id") or node_id
                expanded.append({
                    "connected_id": node_id,
                    "connected_labels": ["Recipe"],
                    "relationship": "SAVED",
                    "payload": payload,
                })
            return {"similar_nodes": [], "expanded_context": expanded}
    except Exception as e:
        logger.warning("retrieve_recipes_from_similar_constraint_users failed: %s", e)
        return {"similar_nodes": [], "expanded_context": []}


def _build_recipe_payload(node_properties: dict[str, Any]) -> dict[str, Any]:
    """Build payload for Recipe node (exclude large arrays)."""
    excluded = ("embedding", "Embedding", "vector", "Vector", "semanticEmbedding")
    payload: dict[str, Any] = {}
    for k, v in node_properties.items():
        if any(k.endswith(s) for s in excluded):
            continue
        if isinstance(v, list) and len(v) > 50 and all(isinstance(x, (int, float)) for x in (v[:5] if v else [])):
            continue
        payload[k] = v
    if "id" not in payload and "r.id" not in payload:
        payload["id"] = node_properties.get("id")
    return payload
