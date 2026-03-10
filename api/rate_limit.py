"""
In-memory rate limiting for the RAG pipeline API.

Protects against cost abuse, data scraping, and DoS by capping requests per identity.
Uses per-minute sliding window. Prunes old entries to avoid unbounded growth.
"""

from __future__ import annotations

import time
from collections import defaultdict

from fastapi import HTTPException

# Config via env (read once at module load)
import os
_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MINUTE", "25"))
_LIMIT_PER_HOUR = int(os.getenv("RATE_LIMIT_PER_HOUR", "300"))
_RETRY_AFTER_SEC = 60

# identity -> list of timestamps (last N requests)
_timestamps: dict[str, list[float]] = defaultdict(list)


def _prune(identity: str, now: float) -> None:
    """Remove timestamps older than 1 hour."""
    keep = now - 3600
    _timestamps[identity][:] = [t for t in _timestamps[identity] if t > keep]
    if not _timestamps[identity]:
        del _timestamps[identity]


def check_rate_limit(identity: str) -> None:
    """
    Raise HTTP 429 if identity has exceeded rate limits.

    Uses per-minute and per-hour caps. Call at the start of protected endpoints.
    """
    identity = str(identity or "anonymous").strip() or "anonymous"
    now = time.time()

    _prune(identity, now)
    ts_list = _timestamps[identity]

    # Per-minute: last 60 seconds
    recent_min = [t for t in ts_list if now - t < 60]
    if len(recent_min) >= _LIMIT_PER_MIN:
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Try again later.",
            headers={"Retry-After": str(_RETRY_AFTER_SEC)},
        )

    # Per-hour
    if len(ts_list) >= _LIMIT_PER_HOUR:
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Try again later.",
            headers={"Retry-After": str(_RETRY_AFTER_SEC)},
        )

    ts_list.append(now)
