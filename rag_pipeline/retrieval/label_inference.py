"""
Label inference for semantic search.

Heuristics first, LLM fallback when heuristics fail. Used when semantic-search
is called without an explicit --label (e.g. CLI, or retrieve_semantic without label).
"""

from __future__ import annotations

import os


def is_valid_label(label: str | None, allowed_labels: list[str]) -> bool:
    """Check if label is in the allowed set (case-insensitive)."""
    if not label or not isinstance(label, str):
        return False
    normalized = label.strip()
    if not normalized:
        return False
    allowed_lower = {a.lower() for a in allowed_labels}
    return normalized.lower() in allowed_lower


def infer_label_with_llm(
    query: str,
    allowed_labels: list[str],
    *,
    model: str | None = None,
) -> str | None:
    """
    Use LLM to infer semantic search label from query.

    Args:
        query: User query text
        allowed_labels: Valid labels (e.g. Recipe, Ingredient, Product, B2C_Customer, Cuisine)
        model: Optional model override (defaults to GENERATION_MODEL env)

    Returns:
        Label if valid, else None
    """
    from openai import OpenAI

    client = OpenAI(
        base_url=os.environ.get("OPENAI_BASE_URL"),
        api_key=os.environ.get("OPENAI_API_KEY"),
    )
    model = model or os.environ.get("GENERATION_MODEL", "openai/gpt-5-mini")
    labels_str = ", ".join(allowed_labels)

    prompt = f"""Given this user query, choose ONE label for semantic search.

Valid labels: {labels_str}

Query: "{query}"

Respond with ONLY the label name, nothing else."""

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=20,
        )
        content = response.choices[0].message.content if response.choices else None
        raw = (content or "").strip()
        # Take first word/token (LLM might add punctuation or extra text)
        label = raw.split()[0] if raw else ""
        # Remove trailing punctuation
        label = label.rstrip(".,;:!?")
        if is_valid_label(label, allowed_labels):
            # Return with correct casing from allowed_labels
            for a in allowed_labels:
                if a.lower() == label.lower():
                    return a
        return None
    except Exception:
        return None
