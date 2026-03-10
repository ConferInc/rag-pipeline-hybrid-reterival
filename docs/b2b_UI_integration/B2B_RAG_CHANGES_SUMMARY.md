# B2B RAG Integration: Current vs Expected Changes

---

## 1. POST /b2b/recommend-products

| Aspect | Currently | Expected (Handoff) |
|--------|-----------|--------------------|
| **health_profile** | `target_calories`, `target_protein_g`, `bmi`; backend passes `filters.maxCalories`, `filters.minProtein` | `derived_limits` (e.g. `sodium_mg`, `sugar_g`), `activity_level`, `health_goal` |
| **Response** | id, name, brand, score, reasons, calories, protein_g, image_url | id, score, reasons (minimal) |

**Needs change?** Only if B2B backend sends `derived_limits`, `activity_level`, `health_goal`. If B2B adapts to current format, no change.

---

## 2. POST /b2b/search

| Aspect | Currently | Expected (Handoff) |
|--------|-----------|--------------------|
| **filters** | maxCalories, minProtein, category, diets, allergen_free | brand, status, category_id |
| **Response** | id, name, brand, score, reasons, query_interpretation, total_found, retrieval_time_ms | id, score, reasons, query_interpretation |

**Needs change?** Only if B2B uses `brand` and `status` in filters. Otherwise no change.

---

## 3. POST /b2b/product-customers

| Aspect | Currently | Expected (Handoff) |
|--------|-----------|--------------------|
| **Customer id field** | `customer_id` | `id` |

**Needs change?** Maybe — B2B may expect `id` for consistency. Could also be handled on B2B side.

---

## 4. POST /b2b/chat

| Aspect | Currently | Expected (Handoff) |
|--------|-----------|--------------------|
| **Export field** | `structured_data` (products/customers) | `report_data` (array of rows for CSV export) |

**Needs change?** Only if B2B expects `report_data` for `/api/v1/chat/export`. Confirm with B2B.

---

## 5. POST /b2b/safety-check

| Aspect | Currently | Expected (Handoff) |
|--------|-----------|--------------------|
| **summary** | Object: `{total_conflicts, critical_count, affected_customers, affected_products}` | String: `"2 conflicts found"` |

**Needs change?** Only if B2B expects a string. If B2B can consume the richer object, no change.

---

## 6. POST /b2b/substitutions

| Aspect | Currently | Expected (Handoff) |
|--------|-----------|--------------------|
| **reason** | `reasons` (list) | `reason` (string) |

**Needs change?** Minor — B2B can use `reasons[0]` if needed.

---

## 7. POST /b2b/product-intel

| Aspect | Currently | Expected (Handoff) |
|--------|-----------|--------------------|
| **ingredients** | ❌ not returned | ✅ array of strings |
| **allergens** | ❌ not returned | ✅ array of strings |
| **customer_suitability** | ❌ not returned | ✅ human-readable string |
| **diet_compatibility** | ✅ returned | ✅ returned |

**Needs change?** **Yes** — this is the main functional gap. Product intel is incomplete without ingredients and allergens.

---

## 8. POST /b2b/search-suggest

| Aspect | Currently | Expected (Handoff) |
|--------|-----------|--------------------|
| **entities_found** | `{ "diet": ["keto"], "allergens": ["peanut"] }` (entity lists) | `{ "products": 5, "allergens": 1 }` (counts) |

**Needs change?** Only if B2B expects numeric counts for display/routing.

---
