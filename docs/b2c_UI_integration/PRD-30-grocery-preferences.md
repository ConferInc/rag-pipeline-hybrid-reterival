# PRD: Grocery Recommendation with User Preferences

> **Tech Stack:** Next.js (frontend), Express + Drizzle ORM (backend), FastAPI (RAG API), Neo4j 5 (graph DB), PostgreSQL `gold` schema  
> **Auth:** Appwrite (B2C auth)  
> **Repos:** `nutrib2c-frontend` (frontend), `nutrition-backend-b2c` (backend), `rag-pipeline-hybrid-reterival` (RAG API)  
> **Family Context:** All features support household/family member switching via `household_id`.  
> **Depends On:** Existing grocery list feature (shopping_lists + shopping_list_items tables)

---

## Overview

Integrate user quality preferences (Organic Only, Non-GMO, No MSG, Grass-Fed, Hormone-Free, Pesticide-Free) and preferred brands into the grocery recommendation pipeline. When generating a grocery list, the system filters/ranks products based on these preferences using Neo4j graph queries (primary) with PostgreSQL as fallback.

**Why this matters:** The current `ragProducts()` only filters by allergen safety. A user who prefers organic products gets the same recommendations as one who doesn't — defeating the purpose of personalization.

**Current State:**

- Backend: `groceryList.ts` → `matchProductsWithRAG()` calls `/recommend/products` with `ingredient_ids + customer_allergens` — NO quality preference params
- RAG: `product_recommendation.py` → `run_recommend_products()` matches Product→Ingredient→Allergen — NO certification filtering
- DB: `product_certifications` table **EXISTS** (31 certifications populated: USDA Organic, Non-GMO, Halal, Kosher, etc.)
- DB: `household_preferences` table exists with types: `organic, non_gmo, dairy_free, gluten_free, low_sodium, sugar_free, vegan, vegetarian, halal, kosher`
- Frontend: Grocery list page has CRUD + generate — NO preference setup wizard

**Figma Designs:**

- Mobile: [841:4664] — Budget setup within grocery-list > View Budget flow
- Desktop Step 2: [888:3939] — Quality & Brands wizard (6 toggles + brand search + meal frequency)
- Desktop Main: [612:7124] — Post-generation grocery list view

## User Stories

| ID | Story | Priority |
|----|-------|----------|
| GP-1 | As a user, I can access a "Preferences" setup from the grocery list page header (accessible anytime, not just onboarding) | P0 |
| GP-2 | As a user, I can toggle quality preferences (Organic Only, Non-GMO, No MSG, Grass-Fed, Hormone-Free, Pesticide-Free) in a 2-step wizard | P0 |
| GP-3 | As a user, I can search and select preferred brands from a dynamically populated autocomplete (querying distinct brands from products table) | P0 |
| GP-4 | As a user, when I generate a grocery list, products are filtered/ranked based on my quality and brand preferences | P0 |
| GP-5 | As a user, I see active preference badges on my grocery list (e.g., "🥬 Organic", "🌱 Non-GMO") | P1 |
| GP-6 | As a user, preferred brand products are boosted to the top of recommendations (soft ranking, not hard filter) | P1 |
| GP-7 | As a user, if no products match all my preferences, I see the best available products with a note about which preferences couldn't be matched | P1 |
| GP-8 | As a user on mobile, I can set quality preferences within the grocery-list > View Budget flow | P0 |

## Critical Analysis: Brand Preferences Storage

### Do we need a `household_brand_preferences` table?

**Recommendation: NO — use `household_preferences` with extended types**

| Approach | Pros | Cons |
|----------|------|------|
| **New `household_brand_preferences` table** | Clean separation; dedicated columns for brand name, is_preferred | Extra table, extra CRUD endpoints, extra migration, more maintenance |
| **Extend `household_preferences` with `preference_type='brand'`** | Reuses existing table + CRUD; `preference_value` stores brand name; `priority` column already exists for ranking | Brand name stored in generic `preference_value` varchar(255) — sufficient for brand names |

**Decision: Extend `household_preferences`**

Rationale:

1. The `household_preferences` table already has `preference_type` + `preference_value` columns — perfect for `preference_type='brand', preference_value='Annie''s'`
2. The existing CRUD service (if any) can be reused
3. `priority` column supports ranking preferred brands
4. Less migration risk — no new table, just widening the CHECK constraint
5. Customer-level preferences aren't needed because grocery shopping is a **household-level activity** (the whole family shares a grocery list)

### Schema Changes

```sql
-- Widen CHECK constraint to add new preference types
ALTER TABLE gold.household_preferences
    DROP CONSTRAINT IF EXISTS household_preferences_preference_type_check;
ALTER TABLE gold.household_preferences
    ADD CONSTRAINT household_preferences_preference_type_check
    CHECK (preference_type IN (
        'organic', 'non_gmo', 'local', 'sustainable', 'budget_conscious',
        'dairy_free', 'gluten_free',
        'no_msg', 'grass_fed', 'hormone_free', 'pesticide_free',  -- NEW quality types
        'brand'  -- NEW: preference_value stores brand name
    ));
```

## Technical Architecture

### Database Tables Used

| Table | Status | Usage |
|-------|--------|-------|
| `gold.household_preferences` | ✅ Exists (extend) | Quality toggles + brand preferences (type='brand', value=brand_name) |
| `gold.product_certifications` | ✅ **EXISTS** | Junction: `product_id → certification_id` (31 certifications populated) |
| `gold.certifications` | ✅ Exists | Master certification list (USDA Organic, Non-GMO, etc.) |
| `gold.products` | ✅ Exists | `brand` column for dynamic autocomplete |
| `gold.shopping_lists` | ✅ Exists | Generated grocery lists |
| `gold.shopping_list_items` | ✅ Exists | Individual items with product associations |

### Backend

#### [NEW] `server/services/groceryPreferences.ts`

CRUD for grocery quality/brand preferences:

```typescript
export async function getGroceryPreferences(householdId: string): Promise<{
  qualityToggles: string[];  // ['organic', 'non_gmo', ...]
  brands: { name: string; priority: number }[];
}>

export async function updateGroceryPreferences(householdId: string, input: {
  qualityToggles: string[];
  brands: { name: string; priority: number }[];
}): Promise<void>

export async function searchBrands(query: string, limit?: number): Promise<string[]>
// → SELECT DISTINCT brand FROM gold.products WHERE brand ILIKE $1 LIMIT 20
```

#### [MODIFY] `server/services/groceryList.ts`

When calling `ragProducts()`, pass quality preferences:

```typescript
const prefs = await getGroceryPreferences(householdId);
const ragResult = await ragProducts({
  ingredient_ids: ingredientIds,
  customer_allergens: allergens,
  quality_preferences: prefs.qualityToggles,
  preferred_brands: prefs.brands.map(b => b.name),
});
```

#### [MODIFY] `server/services/ragClient.ts`

Update `ragProducts()` signature:

```typescript
export async function ragProducts(params: {
  ingredient_ids: string[];
  customer_allergens?: string[];
  quality_preferences?: string[];   // NEW
  preferred_brands?: string[];      // NEW
}): Promise<RagProductResult | null>
```

#### [NEW] `server/routes/groceryPreferences.ts`

```
GET    /api/v1/grocery-preferences           → getGroceryPreferences
PUT    /api/v1/grocery-preferences           → updateGroceryPreferences
GET    /api/v1/grocery-preferences/brands    → searchBrands(?q=)
```

### RAG API (Primary) + SQL Fallback

#### [MODIFY] `api/product_recommendation.py`

```python
def run_recommend_products(
    driver: Driver,
    *,
    ingredient_ids: list[str],
    customer_allergens: list[str] | None = None,
    quality_preferences: list[str] | None = None,  # NEW
    preferred_brands: list[str] | None = None,      # NEW
    database: str | None = None,
) -> dict:
```

**Neo4j filtering (primary):**

```cypher
// Filter by certification
MATCH (p:Product)-[:HAS_CERTIFICATION]->(c:Certification)
WHERE c.code IN $quality_codes
WITH p, collect(c.code) AS certs
WHERE size(certs) >= size($quality_codes)
```

**Brand boosting:** Preferred brands get a +0.2 score boost (soft ranking, not hard filter).

**SQL fallback (when RAG unavailable):**

```sql
SELECT p.* FROM gold.products p
JOIN gold.product_certifications pc ON pc.product_id = p.id
JOIN gold.certifications c ON c.id = pc.certification_id
WHERE c.code IN ('USDA_ORGANIC', 'NON_GMO_PROJECT', ...)
  AND p.brand IN ('Annie''s', 'Kirkland', ...)
```

#### [MODIFY] `api/app.py`

Update `/recommend/products` schema:

```python
class ProductRecommendRequest(BaseModel):
    ingredient_ids: list[str]
    customer_allergens: list[str] | None = None
    quality_preferences: list[str] | None = None   # NEW
    preferred_brands: list[str] | None = None       # NEW
```

### Frontend Changes

| File | Change |
|------|--------|
| **[NEW]** `app/grocery-list/preferences/page.tsx` | 2-step wizard (Budget → Quality & Brands) — accessible from header button |
| `app/grocery-list/grocery-list-client.tsx` | Add "⚙️ Preferences" header button; show active preference badges |
| **[NEW]** `hooks/use-grocery-preferences.ts` | `useGroceryPreferences()`, `useUpdateGroceryPreferences()`, `useSearchBrands()` |
| `lib/api.ts` | Add API calls for preferences endpoints |
| `lib/types.ts` | Add `GroceryPreferences`, `BrandSuggestion` types |

**Wizard Step 2 (matching Figma 888:3939):**

- Section 1: **Ingredient Quality** — 6 toggle switches
- Section 2: **Preferred Brands** — search input with autocomplete, brand pills
- Section 3: **Meal Frequency** — meals/day selector
- Primary CTA: "Generate My List ✨"

**Mobile (matching Figma 841:4664):**

- Accessible within grocery-list > View Budget flow
- Same quality toggles and brand search, optimized for mobile layout

## Acceptance Criteria

- [ ] "Preferences" button accessible from grocery list page header (both mobile and desktop)
- [ ] Quality toggles (Organic, Non-GMO, No MSG, Grass-Fed, Hormone-Free, Pesticide-Free) save to `household_preferences`
- [ ] Brand search autocomplete dynamically queries distinct brands from products table
- [ ] Generating a grocery list with "Organic" preference returns products with USDA_ORGANIC certification
- [ ] Preferred brands are sorted to top of recommendations (not hard-filtered)
- [ ] Active preferences shown as badges on grocery list page
- [ ] If no exact match, best available products shown with preference mismatch note
- [ ] Preferences persist across sessions (refresh → preferences still set)

---

## RAG Team Scope

> **Repo:** `rag-pipeline-hybrid-reterival`  
> **Owner:** RAG Pipeline Engineer

### Deliverables

1. Accept `quality_preferences[]` and `preferred_brands[]` in `/recommend/products`
2. Neo4j Cypher: filter products by `[:HAS_CERTIFICATION]` edges matching quality codes
3. Brand boosting: +0.2 score for preferred brand matches
4. Graceful degradation: if no certified products match, return all products with a `"preference_matched": false` flag

## Route Registration

```typescript
// server/routes/groceryPreferences.ts
import { groceryPreferencesRouter } from "./routes/groceryPreferences.js";
app.use("/api/v1/grocery-preferences", requireAuth, groceryPreferencesRouter);
```

## Environment Variables

No new environment variables — uses existing `USE_GRAPH_PRODUCTS` flag.

## Verification Plan

### Automated Tests

- Unit test `getGroceryPreferences` / `updateGroceryPreferences` CRUD
- Unit test `searchBrands` with partial query matching
- Integration test: generate grocery list with `quality_preferences: ['organic']` → verify filtered results

### Manual Verification

- Navigate to grocery list → click "Preferences" button → complete wizard
- Toggle "Organic Only" → generate list → verify products have organic certification
- Search brands → type "Kirkt" → verify "Kirkland" appears in autocomplete
- Refresh page → verify preferences persist
- Mobile: access preferences from View Budget flow
