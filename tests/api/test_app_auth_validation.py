from __future__ import annotations

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from api.app import SearchRequest, FeedRequest, verify_api_key
import api.rate_limit as rl


@pytest.mark.asyncio
async def test_verify_api_key_rejects_bad_key(monkeypatch):
    monkeypatch.setenv("RAG_API_KEY", "expected-key")
    with pytest.raises(HTTPException) as exc:
        await verify_api_key("bad-key")
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_verify_api_key_accepts_valid_key(monkeypatch):
    monkeypatch.setenv("RAG_API_KEY", "expected-key")
    assert await verify_api_key("expected-key") is None


def test_search_request_invalid_payload_raises_422_equivalent_validation():
    with pytest.raises(ValidationError):
        SearchRequest()  # missing required query


# ── New gap-fill tests ─────────────────────────────────────────────────────────

def test_feed_request_missing_customer_id_raises_validation_error():
    """FeedRequest.customer_id is required — omitting it must raise ValidationError."""
    with pytest.raises(ValidationError):
        FeedRequest()  # customer_id is Field(...) — required


def test_feed_request_with_customer_id_is_valid():
    """FeedRequest with a customer_id should construct without error."""
    req = FeedRequest(customer_id="cust-uuid-123")
    assert req.customer_id == "cust-uuid-123"


def test_rate_limit_per_minute_blocks_at_threshold(monkeypatch):
    """Requests beyond RATE_LIMIT_PER_MINUTE within 60s should raise HTTP 429."""
    identity = "test-user-min"
    limit = 3
    monkeypatch.setattr(rl, "_LIMIT_PER_MIN", limit)
    monkeypatch.setattr(rl, "_LIMIT_PER_HOUR", 10000)  # disable hour limit
    # Reset state for this identity
    rl._timestamps.pop(identity, None)

    frozen = [1_700_000_000.0]
    monkeypatch.setattr(rl.time, "time", lambda: frozen[0])

    # First `limit` requests should pass
    for _ in range(limit):
        rl.check_rate_limit(identity)

    # (limit + 1)th request within same minute must be blocked
    with pytest.raises(HTTPException) as exc:
        rl.check_rate_limit(identity)
    assert exc.value.status_code == 429

    # Cleanup
    rl._timestamps.pop(identity, None)


def test_rate_limit_per_hour_blocks_at_threshold(monkeypatch):
    """Requests beyond RATE_LIMIT_PER_HOUR within 1h should raise HTTP 429."""
    identity = "test-user-hour"
    limit = 5
    monkeypatch.setattr(rl, "_LIMIT_PER_MIN", 10000)   # disable minute limit
    monkeypatch.setattr(rl, "_LIMIT_PER_HOUR", limit)
    rl._timestamps.pop(identity, None)

    base = 1_700_000_000.0

    # Spread requests across the hour window so per-minute limit isn't hit
    for i in range(limit):
        monkeypatch.setattr(rl.time, "time", lambda _i=i: base + _i * 200)
        rl.check_rate_limit(identity)

    # One more — still within the hour window
    monkeypatch.setattr(rl.time, "time", lambda: base + limit * 200)
    with pytest.raises(HTTPException) as exc:
        rl.check_rate_limit(identity)
    assert exc.value.status_code == 429

    # Cleanup
    rl._timestamps.pop(identity, None)
