from __future__ import annotations

from typing import Any

RELATIONSHIP_WEIGHTS: dict[str, int] = {
    "SAVED": 100,
    "RATED": 80,
    "LIKED": 70,
    "VIEWED": 30,
    "TRIED": 50,
    "WHITELISTED": 60,
    "BLACKLISTED": -100,
    "REJECTED": -50,
}

LABEL_DISPLAY_FIELDS: dict[str, list[str]] = {
    "Recipe": ["title", "description", "cuisine_code", "difficulty", "total_time_minutes"],
    "Ingredient": ["name", "category"],
    "Allergens": ["name", "severity"],
    "Dietary_Preferences": ["name", "description"],
    "B2C_Customer": ["full_name", "email"],
    "Cuisine": ["code", "name"],
    "Product": ["name", "brand"],
    "B2C_Customer_Health_Conditions": ["condition_name", "severity"],
    "B2C_Customer_Health_Profiles": ["profile_type", "notes"],
}


def condense_for_llm(
    expanded_context: list[dict[str, Any]],
    *,
    max_items: int = 10,
    include_relationship: bool = True,
) -> list[dict[str, Any]]:
    """
    Condense expanded retrieval results for LLM consumption.

    1. Deduplicates by connected_id (keeps highest-weight relationship)
    2. Ranks by relationship strength
    3. Trims to essential fields per label
    4. Limits to max_items

    Args:
        expanded_context: Raw output from expand_from_seeds()
        max_items: Maximum items to return
        include_relationship: Include relationship type in output

    Returns:
        Condensed list ready for LLM context
    """
    if not expanded_context:
        return []

    node_best: dict[str, dict[str, Any]] = {}

    for item in expanded_context:
        node_id = item["connected_id"]
        rel = item.get("relationship", "")
        weight = RELATIONSHIP_WEIGHTS.get(rel, 0)

        if node_id not in node_best or weight > node_best[node_id]["_weight"]:
            labels = item.get("connected_labels", [])
            label = labels[0] if labels else "Unknown"

            trimmed_payload = _trim_payload(label, item.get("payload", {}))

            node_best[node_id] = {
                "_weight": weight,
                "_relationship": rel,
                "label": label,
                **trimmed_payload,
            }

    sorted_items = sorted(node_best.values(), key=lambda x: x["_weight"], reverse=True)

    result: list[dict[str, Any]] = []
    for item in sorted_items[:max_items]:
        output = {k: v for k, v in item.items() if not k.startswith("_")}
        if include_relationship:
            output["relationship"] = item["_relationship"]
        result.append(output)

    return result


def _trim_payload(label: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Keep only display fields for the given label."""
    fields = LABEL_DISPLAY_FIELDS.get(label)
    if not fields:
        return {k: v for k, v in payload.items() if not _is_large_array(v)}

    trimmed: dict[str, Any] = {}
    for f in fields:
        if f in payload:
            trimmed[f] = payload[f]
    return trimmed


def _is_large_array(value: Any) -> bool:
    """Check if value is a large numeric array (embedding)."""
    if isinstance(value, list) and len(value) > 20:
        if all(isinstance(v, (int, float)) for v in value[:5]):
            return True
    return False


def format_semantic_results_as_text(
    results: list[Any],
    *,
    header: str = "Semantically relevant results:",
    max_items: int = 10,
) -> str:
    """
    Format semantic RetrievalResult objects as human-readable text for LLM prompt.

    Args:
        results: List of RetrievalResult objects from semantic_search_by_label()
        header: Header line for the context block
        max_items: Max number of results to include

    Returns:
        Formatted string ready for LLM prompt
    """
    if not results:
        return ""

    lines = [header]
    for i, r in enumerate(results[:max_items], 1):
        label = r.label
        payload = r.payload
        score = round(r.score_raw, 3)

        if label == "Recipe":
            title = payload.get("title", "Unknown")
            cuisine = payload.get("cuisine_code", "")
            difficulty = payload.get("difficulty", "")
            time_mins = payload.get("total_time_minutes", "")
            desc = (payload.get("description") or "")[:100]
            time_str = f", {time_mins} min" if time_mins else ""
            lines.append(f"{i}. {title} [{cuisine}, {difficulty}{time_str}] (score: {score})")
            if desc:
                lines.append(f"   {desc}...")

        elif label == "Ingredient":
            name = payload.get("name", "Unknown")
            category = payload.get("category", "")
            lines.append(f"{i}. Ingredient: {name} ({category}) (score: {score})")

        elif label == "Product":
            name = payload.get("name", "Unknown")
            brand = payload.get("brand", "")
            lines.append(f"{i}. Product: {name} by {brand} (score: {score})")

        elif label == "B2C_Customer":
            name = payload.get("full_name", "Unknown")
            email = payload.get("email", "")
            lines.append(f"{i}. Customer: {name} ({email}) (score: {score})")

        elif label == "Cuisine":
            code = payload.get("code", "Unknown")
            lines.append(f"{i}. Cuisine: {code} (score: {score})")

        else:
            summary = ", ".join(f"{k}={v}" for k, v in payload.items())
            lines.append(f"{i}. {label}: {summary[:80]} (score: {score})")

    return "\n".join(lines)


def format_context_as_text(
    condensed: list[dict[str, Any]],
    *,
    header: str = "Retrieved context:",
) -> str:
    """
    Format condensed results as human-readable text for LLM prompt.

    Args:
        condensed: Output from condense_for_llm()
        header: Header line for the context block

    Returns:
        Formatted string ready for LLM prompt
    """
    if not condensed:
        return ""

    lines = [header]
    for i, item in enumerate(condensed, 1):
        label = item.get("label", "Item")
        rel = item.get("relationship", "")

        if label == "Recipe":
            title = item.get("title", "Unknown")
            cuisine = item.get("cuisine_code", "")
            difficulty = item.get("difficulty", "")
            desc = item.get("description", "")[:100]
            rel_info = f" ({rel})" if rel else ""
            lines.append(f"{i}. {title} [{cuisine}, {difficulty}]{rel_info}")
            if desc:
                lines.append(f"   {desc}...")

        elif label == "Allergens":
            name = item.get("name", "Unknown allergen")
            lines.append(f"{i}. Allergen: {name}")

        elif label == "Dietary_Preferences":
            name = item.get("name", "Unknown diet")
            lines.append(f"{i}. Diet: {name}")

        elif label == "Ingredient":
            name = item.get("name", "Unknown")
            category = item.get("category", "")
            lines.append(f"{i}. Ingredient: {name} ({category})")

        elif label == "B2C_Customer":
            name = item.get("full_name", "Unknown")
            lines.append(f"{i}. Customer: {name}")

        else:
            summary = ", ".join(f"{k}={v}" for k, v in item.items() if k not in ("label", "relationship"))
            lines.append(f"{i}. {label}: {summary[:80]}")

    return "\n".join(lines)
