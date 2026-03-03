"""
Domain-grounded response generation for the chatbot.

WHY DOMAIN-GROUNDED: The LLM should ONLY respond using data from Neo4j retrieval.
It should NEVER hallucinate recipe names, nutrition facts, or product info.

Two paths:
- Template: greeting, help, farewell, out_of_scope — canned responses, no LLM
- LLM: find_recipe, get_nutritional_info, etc. — uses retrieval context + conversation history
"""

from __future__ import annotations

from typing import Any

from rag_pipeline.augmentation.prompt_builder import build_augmented_prompt
from rag_pipeline.generation.generator import generate_response
from rag_pipeline.orchestrator.orchestrator import OrchestratorResult


# Intents that use canned template responses (no LLM)
TEMPLATE_INTENTS = frozenset({"greeting", "help", "farewell", "out_of_scope"})


def get_template_response(intent: str) -> str:
    """Return canned response for template intents. No LLM call."""
    if intent == "greeting":
        return "Hi! I'm NutriBot. I can help you find recipes, plan meals, check nutrition, and more. What would you like to do?"
    if intent == "help":
        return (
            "I can find recipes (e.g. 'find me a keto dinner'), show your meal plan, "
            "log meals, plan meals for the week, check nutrition info, and answer dietary questions. Try asking something!"
        )
    if intent == "farewell":
        return "Bye! Take care and eat well!"
    if intent == "out_of_scope":
        return (
            "I'm focused on food, recipes, and nutrition. I can help you find recipes, "
            "plan meals, or answer nutrition questions. What would you like to know?"
        )
    return "How can I help you with recipes or nutrition?"


def generate_chat_response(
    orchestrator_result: OrchestratorResult,
    user_message: str,
    conversation_history: str,
    *,
    customer_profile: dict[str, Any] | None = None,
    temperature: float = 0.3,
    max_fused: int = 10,
) -> str:
    """
    Generate a natural language response grounded in retrieval context.

    Uses build_augmented_prompt for context, injects conversation history,
    and calls the LLM. The LLM must only use data from the provided context.

    Args:
        orchestrator_result: Result from orchestrate() with fused retrieval results
        user_message: Current user message
        conversation_history: Formatted prior turns (e.g. "User: ...\\nAssistant: ...")
        customer_profile: For profile section in prompt
        temperature: LLM temperature (lower = more deterministic)
        max_fused: Max fused results to include in context

    Returns:
        Natural language response from LLM
    """
    base_prompt = build_augmented_prompt(
        orchestrator_result,
        user_message,
        customer_profile=customer_profile,
        max_fused=max_fused,
    )

    # Inject conversation history before [USER QUERY] for multi-turn context
    if conversation_history.strip():
        marker = "[USER QUERY]"
        idx = base_prompt.rfind(marker)
        if idx >= 0:
            history_section = f"[CONVERSATION HISTORY]\n{conversation_history.strip()}\n\n"
            base_prompt = base_prompt[:idx] + history_section + base_prompt[idx:]

    return generate_response(base_prompt, temperature=temperature)


def format_conversation_history(messages: list[tuple[str, str]]) -> str:
    """
    Format session history for the LLM prompt.

    Args:
        messages: List of (role, content) — role is 'user' or 'assistant'

    Returns:
        String like "User: hi\\nAssistant: Hello!..."
    """
    lines = []
    for role, content in messages:
        prefix = "User" if role == "user" else "Assistant"
        lines.append(f"{prefix}: {content}")
    return "\n".join(lines)
