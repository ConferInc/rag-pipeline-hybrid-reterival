# B2B ↔ RAG Integration Handoff

**Purpose:** Share B2B endpoints and RAG API contract so the RAG pipeline team can connect.  
**Last Updated:** March 2025

---

## 1. How It Works

```
Frontend (Next.js)  →  B2B Backend (Express)  →  RAG API (FastAPI)
                           ↑
                    X-API-Key auth
                    Circuit breaker
                    Feature flags
```

- **B2B Backend** calls the RAG API with `POST` + `X-API-Key`.
- If RAG is down or flag is OFF, B2B falls back to SQL (no errors to users).
- All RAG calls are scoped by `vendor_id` from B2B auth.

---

## 2. B2B Public Endpoints (What Frontend / Other Clients Call)

These are the B2B backend routes. Auth: `Authorization: Bearer <JWT>` or `X-Appwrite-JWT`.

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | `/products?q=...&limit=...` | Product search (RAG when `q` present) |
| GET | `/products/:id/intel` | Ingredient intelligence |
| GET | `/products/:id/substitutions?limit=10&customer_id=...` | Smart substitutions |
| GET | `/products/:id/matching-customers?limit=50` | Customers safe for this product |
| GET | `/matching/:customerId?limit=...` | Product recommendations for customer |
| POST | `/matching/:customerId/preview` | Preview matches with overrides |
| GET | `/api/v1/search/suggestions?q=...` | "Did you mean?" suggestions |
| POST | `/api/v1/chat` | Chatbot |
| POST | `/api/v1/chat/export` | CSV export from report data |
| POST | `/api/v1/safety-check` | Product–customer safety check |
| GET | `/api/v1/analytics/health-summary` | Health analytics (SQL, no RAG) |
| GET | `/api/v1/admin/rag-status` | Circuit breaker status (admin) |

---

## 3. RAG API Contract (What RAG Team Must Implement)

B2B backend calls these RAG API endpoints. All are `POST` with JSON body. Auth: `X-API-Key: <RAG_API_KEY>`.

**Base URL:** `RAG_API_URL` (e.g. `http://rag-api:8000`)

### 3.1 POST /b2b/recommend-products

**Request:**
```json
{
  "b2b_customer_id": "uuid",
  "vendor_id": "uuid",
  "allergens": ["peanut", "dairy"],
  "health_conditions": ["diabetes", "hypertension"],
  "dietary_preferences": ["keto", "low-sodium"],
  "health_profile": {
    "derived_limits": { "sodium_mg": 2300, "sugar_g": 25 },
    "activity_level": "moderate",
    "health_goal": "weight_loss"
  },
  "limit": 50
}
```

**Expected Response:**
```json
{
  "products": [
    { "id": "product-uuid", "score": 0.95, "reasons": ["No allergen conflicts", "Meets keto"] }
  ]
}
```

**Timeout:** 5s | **Flag:** `USE_GRAPH_RECOMMEND`

---

### 3.2 POST /b2b/search

**Request:**
```json
{
  "query": "keto protein bars",
  "vendor_id": "uuid",
  "filters": { "brand": "...", "status": "active", "category_id": "..." },
  "limit": 50
}
```

**Expected Response:**
```json
{
  "results": [
    { "id": "product-uuid", "score": 0.9, "reasons": ["Matches keto", "High protein"] }
  ],
  "query_interpretation": "keto-friendly protein bars"
}
```

**Timeout:** 3s | **Flag:** `USE_GRAPH_SEARCH`

---

### 3.3 POST /b2b/product-customers

**Request:**
```json
{
  "product_id": "uuid",
  "vendor_id": "uuid",
  "limit": 50
}
```

**Expected Response:**
```json
{
  "customers": [
    { "id": "uuid", "full_name": "...", "email": "...", "safety_status": "safe" }
  ]
}
```

**Timeout:** 5s | **Flag:** `USE_GRAPH_MATCH`

---

### 3.4 POST /b2b/chat

**Request:**
```json
{
  "message": "Show products for diabetics",
  "vendor_id": "uuid",
  "user_id": "uuid",
  "session_id": "uuid-or-null"
}
```

**Expected Response:**
```json
{
  "response": "I found 12 products suitable for diabetic customers...",
  "intent": "b2b_products_for_condition",
  "session_id": "uuid",
  "report_data": []
}
```

- `report_data`: Optional array of row objects when intent is `b2b_generate_report`. B2B sends this to `/api/v1/chat/export` for CSV download.

**Timeout:** 10s | **Flag:** `USE_GRAPH_CHATBOT`

---

### 3.5 POST /b2b/safety-check

**Request:**
```json
{
  "vendor_id": "uuid",
  "product_ids": ["uuid1", "uuid2"],
  "customer_ids": ["uuid1", "uuid2"]
}
```

**Expected Response:**
```json
{
  "conflicts": [
    { "product_id": "...", "customer_id": "...", "reason": "Allergen: peanut" }
  ],
  "summary": "2 conflicts found"
}
```

**Timeout:** 5s | **Flag:** `USE_GRAPH_SAFETY`

---

### 3.6 POST /b2b/substitutions

**Request:**
```json
{
  "product_id": "uuid",
  "vendor_id": "uuid",
  "customer_id": "uuid-or-null",
  "limit": 10
}
```

**Expected Response:**
```json
{
  "substitutes": [
    { "id": "uuid", "name": "...", "reason": "Similar nutrition, peanut-free" }
  ]
}
```

**Timeout:** 5s | **Flag:** `USE_GRAPH_SUBSTITUTE`

---

### 3.7 POST /b2b/product-intel

**Request:**
```json
{
  "product_id": "uuid",
  "vendor_id": "uuid"
}
```

**Expected Response:**
```json
{
  "ingredients": ["...", "..."],
  "allergens": ["peanut", "dairy"],
  "diet_compatibility": ["keto", "gluten-free"],
  "customer_suitability": "Suitable for most diets; avoid if allergic to tree nuts"
}
```

**Timeout:** 3s | **Flag:** `USE_GRAPH_INTEL`

---

### 3.8 POST /b2b/search-suggest

**Request:**
```json
{
  "query": "protien",
  "vendor_id": "uuid"
}
```

**Expected Response:**
```json
{
  "suggestions": ["protein", "protein bar"],
  "entities_found": { "products": 5, "allergens": 1 }
}
```

**Timeout:** 3s | **Flag:** `USE_GRAPH_SEARCH`

---

## 4. B2B Backend Configuration

**Environment variables (B2B backend):**

| Variable | Required | Purpose |
|----------|----------|---------|
| `RAG_API_URL` | Yes (for RAG) | Base URL of RAG API (e.g. `http://rag-api:8000`) |
| `RAG_API_KEY` | Yes (for RAG) | API key sent as `X-API-Key` header |
| `USE_GRAPH_RECOMMEND` | No | Set to `true` to enable |
| `USE_GRAPH_SEARCH` | No | Set to `true` to enable |
| `USE_GRAPH_MATCH` | No | Set to `true` to enable |
| `USE_GRAPH_CHATBOT` | No | Set to `true` to enable |
| `USE_GRAPH_SAFETY` | No | Set to `true` to enable |
| `USE_GRAPH_INTEL` | No | Set to `true` to enable |
| `USE_GRAPH_SUBSTITUTE` | No | Set to `true` to enable |

All flags default to `false`. B2B uses SQL fallback when RAG returns `null` or 4xx/5xx.

---

## 5. What RAG Team Needs to Provide

1. **RAG API base URL** – e.g. `https://rag-api.your-domain.com`
2. **API key** – shared secret for `X-API-Key` header
3. **Implement the 8 endpoints** above with the request/response shapes
4. **Vendor scoping** – all queries must be scoped by `vendor_id`
5. **Health endpoint** – `GET /health` for liveness (optional; B2B uses circuit breaker on failures)

---

## 6. Fallback Behavior

- If RAG returns `null` (timeout, 4xx, 5xx, circuit open, or flag off), B2B uses SQL and returns a response with `fallback: true` where applicable.
- Frontend handles `fallback` by showing "Service temporarily unavailable" or similar.
- No user-facing errors; behavior degrades silently.

---

## 7. Checklist for RAG Team

- [ ] RAG API deployed and reachable from B2B backend
- [ ] All 8 endpoints implemented with correct request/response format
- [ ] `X-API-Key` auth supported
- [ ] `vendor_id` enforced on all queries
- [ ] API key shared with B2B team
- [ ] Base URL shared with B2B team
