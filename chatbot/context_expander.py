"""
Query expansion using conversation context.

Turns follow-up messages like "Can I use soy then?" into standalone queries
by resolving references using prior turns (e.g. "Can I use soy as a vegan substitute
for paneer in recipes?").
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from openai import OpenAI

logger = logging.getLogger(__name__)

# Words that suggest the message is a follow-up (references prior context)
_FOLLOW_UP_CUES = re.compile(
    r"\b(then|it|that|this|instead|otherwise|also|rather|what about|how about|any alternatives?)\b",
    re.I,
)
_MAX_WORDS_FOR_FOLLOW_UP = 12  # Short messages more likely to need expansion


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
) -> str:
    """
    Expand a follow-up message into a standalone query using conversation context.

    When the user says "Can I use soy then?" after discussing paneer recipes,
    this returns something like "Can I use soy as a vegan substitute for paneer
    in recipes?" so NLU and retrieval can understand the full intent.

    Args:
        message: Current user message (may contain pronouns, "then", etc.)
        conversation_history: Prior turns as (role, content) — role is 'user' or 'assistant'

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
            logger.debug(
                "Query expanded",
                extra={"original": message[:50], "expanded": expanded[:80]},
            )
            return expanded
    except Exception as e:
        logger.warning("Query expansion failed, using original: %s", e)

    return message
