"""
Reciprocal Rank Fusion (RRF) for multi-source retrieval results.

Combines semantic, structural, and Cypher results into a single ranked list
using normalized title as the fusion key.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


def _is_uuid(val: Any) -> bool:
    """Return True if val looks like a UUID."""
    if not val or not isinstance(val, str):
        return False
    return bool(
        re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", val.lower())
    )


def _normalize_title(value: Any) -> str:
    """Normalize a string for use as fusion key (lowercase, stripped)."""
    if value is None:
        return ""
    s = str(value).strip().lower()
    return s


def _get_key_from_semantic(r: Any) -> str | None:
    """
    Extract fusion key from semantic RetrievalResult.
    Prefers PostgreSQL UUID (id) when available; falls back to title/name for dedup.
    """
    payload = getattr(r, "payload", {}) or {}
    uid = payload.get("id") or payload.get("r.id")
    if uid and _is_uuid(str(uid)):
        return str(uid)
    key = _normalize_title(payload.get("title") or payload.get("name") or payload.get("code"))
    return key if key else None


def _get_key_from_structural(item: dict[str, Any]) -> str | None:
    """
    Extract fusion key from structural expanded_context item.
    Prefers PostgreSQL UUID (id) when available; never uses elementId as key.
    Falls back to title/name only when no UUID.
    """
    payload = item.get("payload", {}) or {}
    uid = payload.get("id") or payload.get("r.id")
    if uid and _is_uuid(str(uid)):
        return str(uid)
    key = _normalize_title(
        payload.get("title") or payload.get("name") or payload.get("code")
        or item.get("title") or item.get("name") or item.get("code")
    )
    return key if key else None


def _get_key_from_cypher(row: dict[str, Any], intent: str) -> str | None:
    """
    Extract fusion key from a Cypher result row.
    Prefers r.id (UUID) for recipe intents so the API returns hydration-ready IDs.
    Falls back to normalized title for non-recipe intents.
    """
    if intent in ("find_recipe", "find_recipe_by_pantry", "rank_results", "recipes_for_cuisine", "recipes_by_nutrient", "ingredient_in_recipes"):
        # Prefer UUID id; fall back to title for fusion matching with semantic results
        uuid_key = row.get("r.id") or row.get("id")
        if uuid_key:
            return str(uuid_key)
        key = _normalize_title(row.get("r.title") or row.get("title"))
    elif intent in ("get_nutritional_info", "compare_foods", "check_diet_compliance", "nutrient_in_foods", "ingredient_nutrients"):
        key = _normalize_title(row.get("ingredient") or row.get("name"))
    elif intent in ("check_substitution", "get_substitution_suggestion"):
        key = _normalize_title(row.get("suggested_substitute") or row.get("substitute") or row.get("original"))
    elif intent in ("find_product", "product_nutrients"):
        key = _normalize_title(row.get("product") or row.get("p.name") or row.get("name"))
    elif intent in ("cuisine_recipes", "cuisine_hierarchy"):
        key = _normalize_title(row.get("cuisine_name") or row.get("c.name") or row.get("name") or row.get("parent_cuisine"))
    elif intent == "cross_reactive_allergens":
        key = _normalize_title(row.get("a.name") or row.get("name"))
    elif intent == "nutrient_category":
        key = _normalize_title(row.get("nc.category_name") or row.get("category_name") or row.get("display_name"))
    else:
        key = _normalize_title(row.get("title") or row.get("name") or row.get("ingredient") or row.get("product"))
    return key if key else None


def apply_rrf(
    semantic_results: list[Any],
    structural_results: dict[str, Any],
    cypher_results: list[dict[str, Any]],
    intent: str,
    *,
    k: int = 60,
    max_items: int = 15,
) -> list[dict[str, Any]]:
    """
    Fuse semantic, structural, and Cypher results using Reciprocal Rank Fusion.

    Uses normalized title as the fusion key. Items appearing in multiple sources
    receive higher combined scores.

    Args:
        semantic_results: List of RetrievalResult from semantic retrieval
        structural_results: Dict with 'expanded_context' from structural search
        cypher_results: List of row dicts from Cypher retrieval
        intent: Extracted intent (for Cypher key extraction)
        k: RRF constant (default 60)
        max_items: Maximum fused items to return

    Returns:
        List of fused items, each with: key, rrf_score, sources, label, title, payload
    """
    scores: dict[str, float] = {}
    items: dict[str, dict[str, Any]] = {}

    def add(key: str, rank: int, source: str, item_data: dict[str, Any]) -> None:
        if not key:
            return
        contrib = 1.0 / (k + rank)
        scores[key] = scores.get(key, 0.0) + contrib
        if key not in items:
            items[key] = {"key": key, "rrf_score": 0.0, "sources": [], "label": "Unknown", "title": "", "payload": {}}
        items[key]["rrf_score"] = scores[key]
        if source not in items[key]["sources"]:
            items[key]["sources"].append(source)
        # Merge payload: prefer richer data (keep first non-empty)
        existing = items[key]["payload"]
        if not existing and item_data:
            items[key]["payload"] = dict(item_data)
            items[key]["label"] = item_data.get("label", "Unknown")
            items[key]["title"] = item_data.get("title") or item_data.get("name") or key

    # Semantic
    for rank, r in enumerate(semantic_results, 1):
        key = _get_key_from_semantic(r)
        if key:
            payload = getattr(r, "payload", {}) or {}
            label = getattr(r, "label", "Unknown")
            title = payload.get("title") or payload.get("name") or key
            add(key, rank, "semantic", {"label": label, "title": title, **payload})

    # Structural
    expanded = structural_results.get("expanded_context", [])
    condensed = _condense_for_fusion(expanded)
    for rank, item in enumerate(condensed, 1):
        key = _get_key_from_structural(item)
        if key:
            label = item.get("label", "Unknown")
            title = item.get("title") or item.get("name") or key
            add(key, rank, "structural", {"label": label, "title": title, "relationship": item.get("relationship"), **item})

    # Cypher
    def _cypher_label(intent: str, row: dict) -> str:
        if intent in ("find_recipe", "find_recipe_by_pantry", "rank_results", "recipes_for_cuisine", "recipes_by_nutrient", "ingredient_in_recipes", "cuisine_recipes"):
            return "Recipe"
        if intent in ("find_product", "product_nutrients"):
            return "Product"
        if intent in ("cuisine_hierarchy",):
            return "Cuisine"
        if intent == "cross_reactive_allergens":
            return "Allergen"
        if intent == "nutrient_category":
            return "NutritionCategory"
        return "Ingredient"

    for rank, row in enumerate(cypher_results, 1):
        key = _get_key_from_cypher(row, intent)
        if key:
            title = str(
                row.get("r.title") or row.get("title") or row.get("ingredient")
                or row.get("product") or row.get("cuisine_name") or row.get("c.name")
                or row.get("a.name") or row.get("category_name") or row.get("display_name")
                or key
            )
            label = _cypher_label(intent, row)
            # Normalise id field so _merge_results always finds it as "id"
            recipe_id = row.get("r.id") or row.get("id")
            payload = {"label": label, "title": title, **row}
            if recipe_id:
                payload["id"] = str(recipe_id)
            add(key, rank, "cypher", payload)

    # Sort by RRF score descending, limit
    sorted_keys = sorted(scores.keys(), key=lambda x: -scores[x])[:max_items]
    fused = [items[k] for k in sorted_keys]
    logger.debug(
        "RRF fusion complete",
        extra={
            "component": "fusion",
            "intent": intent,
            "semantic_count": len(semantic_results),
            "structural_count": len(structural_results.get("expanded_context", [])),
            "cypher_count": len(cypher_results),
            "fused_count": len(fused),
        },
    )
    return fused


def _condense_for_fusion(expanded_context: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Condense expanded_context for fusion (dedupe by connected_id, keep best)."""
    if not expanded_context:
        return []
    seen: dict[str, dict] = {}
    for item in expanded_context:
        cid = item.get("connected_id")
        if not cid:
            continue
        payload = item.get("payload", {}) or {}
        labels = item.get("connected_labels", [])
        label = labels[0] if labels else "Unknown"
        if cid not in seen:
            seen[cid] = {
                "connected_id": cid,
                "label": label,
                "relationship": item.get("relationship"),
                "payload": payload,
                "title": payload.get("title"),
                "name": payload.get("name"),
            }
    return list(seen.values())


def format_fused_results_as_text(
    fused_results: list[dict[str, Any]],
    *,
    header: str = "Ranked results (semantic + collaborative + graph):",
    max_items: int = 15,
) -> str:
    """
    Format fused RRF results as human-readable text for LLM prompt.

    Args:
        fused_results: Output from apply_rrf()
        header: Header line for the context block
        max_items: Max number of results to include

    Returns:
        Formatted string ready for LLM prompt
    """
    if not fused_results:
        return ""

    lines = [header]
    for i, item in enumerate(fused_results[:max_items], 1):
        title = item.get("title") or item.get("key", "Unknown")
        label = item.get("label", "Item")
        sources = item.get("sources", [])
        payload = item.get("payload", {})
        sources_str = ", ".join(sources) if sources else ""

        if label == "Recipe":
            cuisine = payload.get("cuisine_code", "")
            difficulty = payload.get("difficulty", "")
            rel = payload.get("relationship", "")
            time_mins = payload.get("total_time_minutes", "")
            desc = (payload.get("description") or "")[:100]
            extra = []
            if cuisine:
                extra.append(cuisine)
            if difficulty:
                extra.append(difficulty)
            if rel:
                extra.append(rel)
            if time_mins:
                extra.append(f"{time_mins} min")
            extra_str = f" [{', '.join(extra)}]" if extra else ""
            lines.append(f"{i}. {title}{extra_str} (sources: {sources_str})")
            if desc:
                lines.append(f"   {desc}...")
        elif label == "Ingredient":
            category = payload.get("category", "")
            cat_str = f" ({category})" if category else ""
            lines.append(f"{i}. Ingredient: {title}{cat_str} (sources: {sources_str})")
        elif label == "Product":
            brand = payload.get("brand", "")
            brand_str = f" [{brand}]" if brand else ""
            lines.append(f"{i}. Product: {title}{brand_str} (sources: {sources_str})")
        elif label in ("Cuisine", "Allergen", "NutritionCategory"):
            lines.append(f"{i}. {label}: {title} (sources: {sources_str})")
        else:
            lines.append(f"{i}. {title} (sources: {sources_str})")

    return "\n".join(lines)
