"""
B2B chat session store — in-memory, TTL 30 min.
Optional PG persistence can be added later.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

TTL_SECONDS = 30 * 60  # 30 min


@dataclass
class B2BSession:
    session_id: str
    vendor_id: str
    messages: deque[tuple[str, str]] = field(default_factory=lambda: deque(maxlen=10))
    created_at: float = field(default_factory=time.time)

    def add(self, role: str, content: str) -> None:
        self.messages.append((role, content))

    def is_expired(self) -> bool:
        return time.time() - self.created_at > TTL_SECONDS

    def to_context(self) -> list[dict[str, str]]:
        return [{"role": r, "content": c} for r, c in self.messages]


_sessions: dict[str, B2BSession] = {}


def get_or_create_session(session_id: str | None, vendor_id: str) -> B2BSession:
    import uuid
    sid = session_id or str(uuid.uuid4())
    if sid in _sessions:
        s = _sessions[sid]
        if s.is_expired():
            del _sessions[sid]
            s = B2BSession(session_id=sid, vendor_id=vendor_id)
            _sessions[sid] = s
        return s
    s = B2BSession(session_id=sid, vendor_id=vendor_id)
    _sessions[sid] = s
    return s


def add_message(session_id: str, role: str, content: str) -> None:
    if session_id in _sessions:
        _sessions[session_id].add(role, content)
