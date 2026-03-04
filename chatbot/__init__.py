"""
Chatbot engine for the B2C nutrition app.

Processes natural language messages via NLU → intent routing → graph queries → LLM response.
Session management, action orchestration, and response generation live here.
"""

from chatbot.action_orchestrator import (
    is_confirmation_message,
    is_rejection_message,
    route_intent,
)
from chatbot.nlu import NLUResult, extract_hybrid
from chatbot.response_generator import (
    TEMPLATE_INTENTS,
    format_conversation_history,
    generate_chat_response,
    get_template_response,
)
from chatbot.session import (
    ChatMessage,
    ChatSession,
    PendingAction,
    get_or_create_session,
    cleanup_expired,
)

__all__ = [
    "is_confirmation_message",
    "is_rejection_message",
    "route_intent",
    "NLUResult",
    "extract_hybrid",
    "TEMPLATE_INTENTS",
    "get_template_response",
    "generate_chat_response",
    "format_conversation_history",
    "ChatMessage",
    "ChatSession",
    "PendingAction",
    "get_or_create_session",
    "cleanup_expired",
]
