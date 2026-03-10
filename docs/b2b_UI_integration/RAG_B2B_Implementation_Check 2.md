# RAG ↔ B2B Implementation Check

**Purpose:** Compare RAG's current implementation, Handoff expectations, and B2B implementation. Identify gaps and resolution options.

**Sources:**
- [B2B RAG Integration_ Current vs Expected Changes.md](B2B%20RAG%20Integration_%20Current%20vs%20Expected%20Changes.md) — RAG Current vs Expected
- [RAG_INTEGRATION_HANDOFF.md](B2B%20RAG%20Integration/RAG_INTEGRATION_HANDOFF.md) — B2B contract
- B2B backend: `nutri-b2b-backend/server/routes.ts`, `ragClient.ts`

---

## 1. POST /b2b/recommend-products

| Aspect | RAG Currently | Expected (Handoff) | B2B Implementation |
|--------|---------------|--------------------|----------------------|
| **health_profile** | `target_calories`, `target_protein_g`, `bmi`; `filters.maxCalories`, `filters.minProtein` | `derived_limits`, `activity_level`, `health_goal` | B2B sends Handoff format: `derived_limits`, `activity_level`, `health_goal` |
| **Response** | id, name, brand, score, reasons, calories, protein_g, image_url | id, score, reasons (minimal) | B2B uses `r.id`, `r.score`, `r.reasons`; enriches via `getProduct()`. No dependency on RAG returning name/brand/calories. |

**Needs change?** RAG expects different request format than B2B sends. B2B will not work optimally until RAG accepts `derived_limits`, `activity_level`, `health_goal` — or B2B adds `target_calories`, `target_protein_g`, `bmi` to the request.

**Resolution:** RAG adapts to Handoff format, or B2B sends both formats.

---

## 2. POST /b2b/search

| Aspect | RAG Currently | Expected (Handoff) | B2B Implementation |
|--------|---------------|--------------------|----------------------|
| **filters** | maxCalories, minProtein, category, diets, allergen_free | brand, status, category_id | B2B sends `filters: { brand, status, category_id }` |
| **Response** | id, name, brand, score, reasons, query_interpretation, total_found, retrieval_time_ms | id, score, reasons, query_interpretation | B2B uses `results`, `r.id`, `r.score`, `r.reasons`, `query_interpretation`. Extra RAG fields are fine. |

**Needs change?** RAG expects nutrition/health filters; B2B sends catalog filters. RAG may ignore B2B filters.

**Resolution:** RAG adds support for `brand`, `status`, `category_id`, or B2B adds optional nutrition filters to search.

---

## 3. POST /b2b/product-customers

| Aspect | RAG Currently | Expected (Handoff) | B2B Implementation |
|--------|---------------|--------------------|----------------------|
| **Customer id field** | `customer_id` | `id` | B2B passes through `ragResult`. Frontend uses `full_name`, `name`, `email`, `safety_status` — does not use id for list display. |

**Needs change?** Minor. RAG returns `customer_id`; Handoff expects `id`. B2B is agnostic.

**Resolution:** RAG adds `id` (or aliases `customer_id` → `id`) for consistency. B2B could add a response adapter mapping `customer_id` → `id` if needed.

---

## 4. POST /b2b/chat

| Aspect | RAG Currently | Expected (Handoff) | B2B Implementation |
|--------|---------------|--------------------|----------------------|
| **Export field** | `structured_data` (products/customers) | `report_data` (array of rows for CSV export) | B2B expects `report_data`. Chat handler stores `report_data` or converted `structured_data` in session. Export accepts `report_data` in body or `session_id` for session-based retrieval. |

**Needs change?** RAG returns `structured_data`; B2B expects `report_data`. B2B now converts `structured_data` → rows and stores for session-based export.

**Resolution:** B2B adapter in place: `structuredDataToReportRows()` converts RAG's `structured_data` to row array. RAG can also return `report_data` directly for Handoff alignment.

---

## 5. POST /b2b/safety-check

| Aspect | RAG Currently | Expected (Handoff) | B2B Implementation |
|--------|---------------|--------------------|----------------------|
| **summary** | Object: `{total_conflicts, critical_count, affected_customers, affected_products}` | String: `"2 conflicts found"` | B2B passes through. Frontend displays `result.summary` as string — object would show `[object Object]`. |

**Needs change?** RAG returns object; frontend expects string.

**Resolution:** B2B adapter: normalize object → string (e.g. `${obj.total_conflicts} conflicts found`) before returning. Or RAG adds string `summary` field.

---

## 6. POST /b2b/substitutions

| Aspect | RAG Currently | Expected (Handoff) | B2B Implementation |
|--------|---------------|--------------------|----------------------|
| **reason** | `reasons` (list) | `reason` (string) | B2B passes through. Frontend displays `s.reason` — with `reasons` list, nothing shows. |

**Needs change?** Minor. RAG returns `reasons`; frontend expects `reason`.

**Resolution:** B2B adapter: map `reasons?.[0]` → `reason` before returning. Or RAG returns `reason` (string).

---

## 7. POST /b2b/product-intel

| Aspect | RAG Currently | Expected (Handoff) | B2B Implementation |
|--------|---------------|--------------------|----------------------|
| **ingredients** | ❌ not returned | ✅ array of strings | B2B expects it. SQL fallback provides from product. When RAG used, intel tab incomplete if RAG omits. |
| **allergens** | ❌ not returned | ✅ array of strings | Same as above. |
| **customer_suitability** | ❌ not returned | ✅ human-readable string | Same. Fallback uses `null`. |
| **diet_compatibility** | ✅ returned | ✅ returned | Aligned. |

**Needs change?** **Yes** — main functional gap. RAG does not return ingredients, allergens, customer_suitability.

**Resolution:** RAG adds these fields. Or B2B merges RAG response with product row (ingredients, allergens from product when RAG omits).

---

## 8. POST /b2b/search-suggest

| Aspect | RAG Currently | Expected (Handoff) | B2B Implementation |
|--------|---------------|--------------------|----------------------|
| **entities_found** | `{ "diet": ["keto"], "allergens": ["peanut"] }` (entity lists) | `{ "products": 5, "allergens": 1 }` (counts) | B2B passes through. Fallback uses `entities_found: null`. |

**Needs change?** Only if B2B frontend expects numeric counts for display.

**Resolution:** RAG returns counts per Handoff, or B2B maps entity lists → counts if needed.

---

## Summary

| Endpoint | RAG ↔ Expected | B2B ↔ Expected | Action |
|----------|----------------|----------------|--------|
| recommend-products | Mismatch (request) | B2B aligned | RAG or B2B adapt request format |
| search | Mismatch (filters) | B2B aligned | RAG or B2B adapt filters |
| product-customers | Mismatch (id field) | B2B agnostic | RAG add `id`; or B2B adapter |
| chat | Mismatch (report_data) | B2B has adapter | B2B converts structured_data; RAG can add report_data |
| safety-check | Mismatch (summary) | Frontend expects string | B2B adapter or RAG add string |
| substitutions | Mismatch (reason) | Frontend expects reason | B2B adapter or RAG add reason |
| product-intel | **Gap** (missing fields) | B2B expects all | RAG add ingredients, allergens, customer_suitability; or B2B merge with product |
| search-suggest | Mismatch (entities_found) | B2B agnostic | RAG or B2B adapt if frontend needs counts |

---

## B2B Adapters Implemented

| Endpoint | Adapter | Status |
|----------|---------|--------|
| chat | `structuredDataToReportRows()` + session store for export | Done |
| safety-check | — | Not implemented |
| substitutions | — | Not implemented |
| product-intel | — | Could merge RAG + product when RAG omits fields |

## B2B Adapters Recommended

1. **safety-check:** Normalize `summary` object → string when `typeof summary === 'object'`
2. **substitutions:** Map `reasons?.[0]` → `reason` per substitute
3. **product-intel:** Merge RAG response with product (ingredients, allergens from product when RAG omits)
