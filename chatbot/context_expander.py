"""
Query expansion using conversation context.

Turns follow-up messages like "Can I use soy then?" into standalone queries
by resolving references using prior turns (e.g. "Can I use soy as a vegan substitute
for paneer in recipes?"). Also handles "some more options?" after substitution results
with retries and a simple keyword-based fallback.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

from openai import OpenAI

logger = logging.getLogger(__name__)

# Words that suggest the message is a follow-up (references prior context)
_FOLLOW_UP_CUES = re.compile(
    r"\b(then|it|that|this|instead|otherwise|also|rather|what about|how about|"
    r"any alternatives?|more options?|more substitutes?|some more|other options?)\b",
    re.I,
)
_MAX_WORDS_FOR_FOLLOW_UP = 12  # Short messages more likely to need expansion

# Phrases that mean "give me more substitution options"
_MORE_OPTIONS_PATTERN = re.compile(
    r"\b(more\s+(options?|substitutes?|alternatives?)|"
    r"other\s+(options?|alternatives?|substitutes?)|"
    r"some\s+more|(any|any\s+other)\s+(more\s+)?(options?|alternatives?)|"
    r"alternatives?\s*\??|something\s+else|anything\s+else)\b",
    re.I,
)


def _looks_like_follow_up(message: str) -> bool:
    """True if the message likely references prior context."""
    msg = (message or "").strip()
    if not msg:
        return False
    word_count = len(msg.split())
    return (
        word_count <= _MAX_WORDS_FOR_FOLLOW_UP
        or _FOLLOW_UP_CUES.search(msg) is not None
    )


def _is_substitution_context(history: list[tuple[str, str]]) -> bool:
    """True if the last assistant message looks like a substitution result."""
    for role, content in reversed(history):
        if role == "assistant":
            c = content.lower()
            return any(
                kw in c for kw in ("substitute", "alternatives", "instead of", "you can use", "replace")
            )
    return False


def _looks_like_more_options_request(message: str) -> bool:
    """True if the message asks for more options/alternatives."""
    return _MORE_OPTIONS_PATTERN.search((message or "").strip()) is not None


def _extract_ingredient_from_history(history: list[tuple[str, str]]) -> str | None:
    """
    Extract the original ingredient from recent substitution turns.
    Used for fallback when LLM expansion fails.
    """
    text_parts: list[str] = []
    for role, content in history[-4:]:
        text_parts.append(content)
    combined = " ".join(text_parts)

    for pat in (
        r"alternatives?\s+(?:to|for)\s+([a-z][a-z\s]{1,30}?)(?:\?|\.|$|\s+(?:in|for)\b)",
        r"substitutes?\s+for\s+([a-z][a-z\s]{1,30}?)(?:\?|\.|$|\s+(?:in|for)\b)",
        r"(?:for|as\s+a\s+substitute\s+for)\s+([a-z][a-z\s]{1,30}?)(?:\s+you\s+can|\s+try|,|\.|$)",
        r"replace\s+([a-z][a-z\s]{1,20}?)\s+with",
        r"instead\s+of\s+([a-z][a-z\s]{1,20}?)(?:\s|,|\.|$)",
    ):
        m = re.search(pat, combined, re.I)
        if m:
            ing = m.group(1).strip()
            if 2 <= len(ing) <= 40 and ing.lower() not in ("the", "a", "it"):
                return ing
    return None


def _format_history(history: list[tuple[str, str]]) -> str:
    """Format conversation history for the LLM."""
    lines = []
    for role, content in history:
        prefix = "User" if role == "user" else "Assistant"
        lines.append(f"{prefix}: {content}")
    return "\n".join(lines)


def expand_query_with_context(
    message: str,
    conversation_history: list[tuple[str, str]],
    max_retries: int = 2,
) -> str:
    """
    Expand a follow-up message into a standalone query using conversation context.

    When the user says "Can I use soy then?" after discussing paneer recipes,
    this returns something like "Can I use soy as a vegan substitute for paneer
    in recipes?" so NLU and retrieval can understand the full intent.

    Uses retries on LLM failure and a simple keyword-based fallback when the
    last turn was a substitution result and the user asks for "more options".

    Args:
        message: Current user message (may contain pronouns, "then", etc.)
        conversation_history: Prior turns as (role, content) — role is 'user' or 'assistant'
        max_retries: Number of retries on LLM failure (default 2).

    Returns:
        Expanded query, or the original message if expansion is skipped/fails.
    """
    message = (message or "").strip()
    if not message:
        return message

    if not conversation_history:
        return message

    if not _looks_like_follow_up(message):
        return message

    history_text = _format_history(conversation_history[-6:])  # Last 3 turns
    prompt = f"""Given this conversation about food, recipes, or nutrition:

{history_text}

User (current message): {message}

Reformulate the user's current message into a clear, standalone query that captures their full intent. Resolve pronouns (it, that, this), "then", "instead", etc. using context. If the message is already clear and standalone, return it unchanged. Return ONLY the expanded query, no explanation."""

    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            client = OpenAI(
                base_url=os.environ.get("OPENAI_BASE_URL"),
                api_key=os.environ.get("OPENAI_API_KEY"),
                timeout=float(os.environ.get("LLM_TIMEOUT", "30")),
            )
            model = os.environ.get("INTENT_MODEL", "gpt-4o-mini")
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                max_tokens=150,
            )
            expanded = (response.choices[0].message.content or "").strip()
            if expanded:
                # If LLM returned essentially the same short "more options" message
                # and we're in substitution context, use our fallback for a fuller query
                if (
                    len(expanded.split()) <= _MAX_WORDS_FOR_FOLLOW_UP
                    and _looks_like_more_options_request(expanded)
                    and _is_substitution_context(conversation_history)
                ):
                    ingredient = _extract_ingredient_from_history(conversation_history)
                    if ingredient:
                        fallback = f"What are more substitutes for {ingredient}?"
                        logger.debug(
                            "Query expansion: LLM returned short reply, using fallback",
                            extra={"llm_expanded": expanded[:50], "fallback": fallback},
                        )
                        return fallback
                logger.debug(
                    "Query expanded",
                    extra={"original": message[:50], "expanded": expanded[:80]},
                )
                return expanded
        except Exception as e:
            last_error = e
            logger.warning("Query expansion attempt %d failed: %s", attempt + 1, e)
            if attempt < max_retries:
                time.sleep(0.5 * (attempt + 1))  # Brief backoff before retry

    # Fallback: if last turn was substitution and user asks for more options,
    # build a simple query from history
    if _is_substitution_context(conversation_history) and _looks_like_more_options_request(message):
        ingredient = _extract_ingredient_from_history(conversation_history)
        if ingredient:
            fallback = f"What are more substitutes for {ingredient}?"
            logger.debug(
                "Query expansion fallback (substitution context)",
                extra={"original": message[:50], "fallback": fallback},
            )
            return fallback

    if last_error:
        logger.warning("Query expansion failed after %d retries, using original: %s", max_retries + 1, last_error)
    return message
