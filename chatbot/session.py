"""
Chat session management for the chatbot.

Multi-turn conversation needs memory. When a user says "swap Tuesday dinner",
the bot needs to know which meal plan they're referring to (from a previous message).

Storage: In-memory dict for MVP, move to Redis when scaling.
Session expires after 30 min of inactivity.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any


@dataclass
class ChatMessage:
    """Single message in the conversation history."""
    role: str  # "user" | "assistant" | "system"
    content: str
    intent: str | None = None
    entities: dict[str, Any] | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class PendingAction:
    """Action awaiting user confirmation before execution."""
    action_id: str
    intent: str
    entities: dict[str, Any]
    preview: dict[str, Any]  # What to show the user for confirmation


@dataclass
class ChatSession:
    """Chat session for one customer, tracks history and pending actions."""
    session_id: str
    customer_id: str
    history: list[ChatMessage] = field(default_factory=list)
    pending_action: PendingAction | None = None
    # API payload {type, params} for Express to execute on confirmation
    pending_action_payload: dict[str, Any] | None = None
    current_meal_plan_id: str | None = None
    current_grocery_list_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_activity: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_expired(self) -> bool:
        """True if session has been inactive for more than 30 minutes."""
        return datetime.now(timezone.utc) - self.last_activity > timedelta(minutes=30)

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        """Add a message to history and update last_activity."""
        self.history.append(ChatMessage(role=role, content=content, **kwargs))
        # Keep last 10 messages (sliding window for LLM context)
        if len(self.history) > 10:
            self.history = self.history[-10:]
        self.last_activity = datetime.now(timezone.utc)


# In-memory session store (MVP). Replace with Redis for production.
_sessions: dict[str, ChatSession] = {}


def get_or_create_session(customer_id: str, session_id: str | None = None) -> ChatSession:
    """
    Get existing session by session_id, or create a new one.

    If session_id is provided and valid (exists, not expired, same customer),
    returns that session. Otherwise creates a new session.
    """
    if session_id and session_id in _sessions:
        session = _sessions[session_id]
        if not session.is_expired and session.customer_id == customer_id:
            return session

    # Create new session
    new_id = session_id or str(uuid.uuid4())
    session = ChatSession(session_id=new_id, customer_id=customer_id)
    _sessions[new_id] = session
    return session


def cleanup_expired() -> None:
    """Remove expired sessions. Call periodically (e.g. on each request)."""
    expired = [sid for sid, s in _sessions.items() if s.is_expired]
    for sid in expired:
        del _sessions[sid]
