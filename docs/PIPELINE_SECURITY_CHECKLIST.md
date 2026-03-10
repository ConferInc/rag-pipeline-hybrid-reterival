# Pipeline Code Security Checklist

Concrete items that can be added **inside the RAG pipeline** to handle abuse and cost in real time. Use this when implementing the [Security & Rate Limiting Plan](./SECURITY_AND_RATE_LIMITING_PLAN.md).

---

## Quick Reference

| Area | Component | Where to Add | Effort |
|------|-----------|--------------|--------|
| Validation | Max length | Pydantic `Field` | S |
| Validation | Request body size | Middleware | S |
| Rate limit | In-memory limiter | Dependency | S |
| Rate limit | Redis-backed limiter | Dependency | M |
| Logging | Non-PII structured log | Endpoint / middleware | S |
| Resilience | Timeouts | Config / client init | S |
| Resilience | Circuit breaker | Around LLM/Neo4j | M |

S = Small, M = Medium

---

## 1. Input Validation (Existing + Extensible)

### Already in place

- `SearchRequest.query`: `Field(..., max_length=500)`
- `ChatProcessRequest.message`: validate length in Pydantic

### Add

- **`ChatProcessRequest.message`** — `Field(..., max_length=1000)` if not set
- **Request body size** — Middleware that rejects requests > 64 KB
- **Query sanitization** — Optional: block obviously malicious patterns (e.g. long repeated strings, control chars)

**Files:** `api/app.py` (request models)

---

## 2. Rate Limiting Dependency

### In-memory (no Redis)

```python
# New: api/rate_limit.py
from collections import defaultdict
from time import time

_window = defaultdict(list)  # key -> [timestamp, ...]

def check_rate_limit(identity: str, limit_per_min: int = 25) -> None:
    now = time()
    window = _window[identity]
    window[:] = [t for t in window if now - t < 60]
    if len(window) >= limit_per_min:
        raise HTTPException(429, "Rate limit exceeded")
    window.append(now)
```

- **Identity:** `customer_id or session_id or "anonymous"`
- **Use:** `Depends(check_rate_limit)` on chat, search, recommend endpoints

### Redis-backed (for multi-replica)

- Use sliding window or token bucket in Redis
- Key: `rl:{identity}:min`, `rl:{identity}:hr`, `rl:{identity}:day`
- Atomic increment + expiry via `INCR` + `EXPIRE` or Lua script

**Files:** New `api/rate_limit.py`, wire into `api/app.py`

---

## 3. Non-PII Logging

### Add after each sensitive request

```python
logger.info(
    "request_complete",
    extra={
        "endpoint": "chat_process",
        "customer_id": req.customer_id[:8] + "..." if req.customer_id else "anon",
        "session_id": session.session_id[:8] + "..." if session else None,
        "intent": nlu_result.intent,
        "latency_ms": (time.time() - start) * 1000,
    },
)
```

- Do **not** log: `req.message`, `req.query`, full `customer_id`
- Do log: truncated IDs, intent, endpoint, latency, rate-limit hits

**Files:** `api/app.py` (endpoint handlers)

---

## 4. Timeouts

### LLM client

```python
OpenAI(
    ...,
    timeout=float(os.getenv("LLM_TIMEOUT", 30)),
)
```

### Neo4j

- Driver / session level: configure `connection_timeout`, `max_connection_lifetime`
- Or wrap calls in `asyncio.wait_for(..., timeout=10)`

**Files:** `api/app.py` (lifespan), Neo4j driver init

---

## 5. 429 Handler

### Central exception handler

```python
@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    if exc.status_code == 429:
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded. Try again later."},
            headers={"Retry-After": "60"},
        )
    raise exc
```

**Files:** `api/app.py`

---

## 6. Integration Points Summary

| Endpoint | Rate Limit | Log | Timeout |
|----------|------------|-----|---------|
| `POST /chat/process` | ✓ | ✓ | ✓ |
| `POST /search/hybrid` | ✓ | ✓ | ✓ |
| `POST /recommend/feed` | ✓ | ✓ | ✓ |
| `POST /recommend/meal-candidates` | ✓ | ✓ | ✓ |
| `POST /recommend/products` | ✓ | ✓ | ✓ |
| `POST /recommend/alternatives` | ✓ | ✓ | ✓ |
| `POST /substitutions/ingredient` | ✓ | ✓ | ✓ |
| B2B routes | Per vendor/key | ✓ | ✓ |

---

## 7. Optional: Circuit Breaker

When LLM or Neo4j error rate exceeds threshold (e.g. 5 errors in 1 min), temporarily reject or return fallback responses instead of calling the failing service.

**Library:** `pybreaker` or custom state machine  
**Placement:** Around `generate_chat_response`, `extract_hybrid`, Neo4j session usage

---

## 8. Optional: Query Fingerprinting

For scraping detection:

- Normalize query (lowercase, trim)
- Optional: compute hash
- Store recent hashes per `customer_id` in Redis or in-memory
- Flag when > N unique queries in short window (e.g. 50 in 5 min)

**Placement:** Before NLU, inside rate limit dependency or middleware
