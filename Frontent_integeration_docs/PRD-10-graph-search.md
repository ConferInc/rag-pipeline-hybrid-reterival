# PRD 10: Graph-Enhanced Search

> **Tech Stack:** Next.js (frontend), Express + Drizzle ORM (backend), FastAPI (RAG API), Neo4j 5 (graph DB), PostgreSQL `gold` schema  
> **Auth:** Appwrite (B2C auth)  
> **Repos:** `nutrib2c-frontend` (frontend), `nutrition-backend-b2c` (backend), `rag-pipeline-hybrid-reterival` (RAG API)  
> **Family Context:** All features support household/family member switching. The `households` table is the root entity; each member is a `b2c_customers` row linked via `household_id`.  
> **Depends On:** PRD-09 (Foundation & Resilience)

---

## 10.1 Overview

Upgrade recipe search from SQL-only keyword matching to a hybrid system: when the user types a natural language query (e.g., "high protein vegan dinner under 30 minutes"), the RAG API performs semantic + structural graph search for better results. When the user only uses filters (cuisine dropdown, calorie slider), the existing SQL search handles it directly — it's faster for indexed columns.

**Why this matters:** The current `searchRecipes()` in `search.ts` does SQL `ILIKE` matching on recipe titles/descriptions. It can't understand semantic queries like "something light for a summer evening" or "kid-friendly lunches without nuts." The RAG pipeline's hybrid search combines vector similarity (sentence embeddings) with graph structure (diet compliance, allergen safety, cuisine relationships) for significantly better recall and ranking.

**Current State:**

- Backend: `server/services/search.ts` → `searchRecipes()` (291 lines) — full SQL filter search with diet/allergen/nutrition filtering. Works perfectly, just not semantic.
- Frontend: Search page exists with filter controls. No NL search hints.

**SQL Fallback:** If `USE_GRAPH_SEARCH=false` or RAG API is down → silently falls back to existing `searchRecipes()`. UI looks identical, results are just keyword-matched instead of semantically ranked.

## 10.2 User Stories

| ID | Story | Priority |
|----|-------|----------|
| SR-1 | As a user, I type "high protein vegan dinner" and get relevant recipes even if those exact words aren't in the title | P0 |
| SR-2 | As a user, I see match reason badges on results ("Matches your Vegan diet", "High protein") | P0 |
| SR-3 | As a user, I can still use traditional filter-based search (cuisine, calories, etc.) | P0 |
| SR-4 | As a user, when my NL search returns < 3 results, I see semantic suggestions ("Did you mean...?") | P1 |
| SR-5 | As a user, search works normally even if the graph database is down | P0 |
| SR-6 | As a user, I see NL search hints below the search bar to guide me | P1 |

## 10.3 Technical Architecture

### 10.3.1 Backend API

#### [MODIFY] `server/services/search.ts`

Add `searchRecipesWithRAG()` alongside existing `searchRecipes()`:

```typescript
import { ragSearch } from "./ragClient.js";

export async function searchRecipesWithRAG(params: SearchParams, userId: string) {
  // Strategy: If user typed a natural language query, try RAG first.
  // If RAG is down/disabled, silently fall back to SQL.
  // If filters only (no free text), always use SQL — it's faster for indexed columns.

  if (params.q) {
    const ragResult = await ragSearch(params.q, params, userId);
    if (ragResult) {
      // RAG returned results — hydrate with full recipe data from PG
      // (Neo4j doesn't store images, full nutrition, etc.)
      const ids = ragResult.results.map((r: any) => r.id);
      const hydrated = await hydrateRecipesByIds(ids);
      return hydrated.map((recipe, i) => ({
        recipe,
        score: ragResult.results[i]?.score ?? 0,
        reasons: ragResult.results[i]?.reasons ?? [],
      }));
    }
    // ragResult is null → RAG unavailable, fall through to SQL
  }

  // SQL fallback (or filter-only search)
  return searchRecipes(params);
}
```

#### [NEW] `server/services/recipeHydration.ts` — Add function

```typescript
export async function hydrateRecipesByIds(ids: string[]): Promise<any[]> {
  // Fetch full recipe data from PG for a list of IDs
  // Preserves the order of the input IDs (important — RAG ranked them)
  const rows = await executeRaw(`
    SELECT r.*, c.id AS cuisine_id, c.code AS cuisine_code, c.name AS cuisine_name
    FROM gold.recipes r
    LEFT JOIN gold.cuisines c ON c.id = r.cuisine_id
    WHERE r.id = ANY($1)
  `, [ids]);
  
  // Re-order to match RAG ranking
  const map = new Map(rows.map((r: any) => [r.id, r]));
  return ids.map(id => map.get(id)).filter(Boolean);
}
```

#### [MODIFY] `server/routes/recipes.ts`

```typescript
// Change search endpoint to use graph-enhanced search:
router.get("/search", requireAuth, async (req, res) => {
  const params = parseSearchParams(req.query);
  const results = await searchRecipesWithRAG(params, req.user.id);
  res.json({ results, total: results.length });
});
```

### 10.3.2 RAG API Endpoint

`POST /search/hybrid` (already defined in RAG pipeline):

```json
// Request
{
  "query": "high protein vegan dinner under 30 minutes",
  "filters": { "diets": ["vegan"], "proteinMin": 25, "timeMax": 30 },
  "customer_id": "uuid"
}

// Response
{
  "results": [
    {
      "id": "recipe-uuid",
      "score": 0.92,
      "reasons": ["Matches your Vegan diet", "32g protein per serving", "25 min total time"],
      "match_type": "semantic+structural"
    }
  ],
  "query_interpretation": "Looking for high-protein vegan dinner recipes with max 30 min cooking time"
}
```

### 10.3.3 Database Tables Used (Already Exist)

| Table | Usage |
|-------|-------|
| `gold.recipes` | Hydrate RAG results with full recipe data |
| `gold.cuisines` | Cuisine names for display |
| `gold.recipe_nutrition_profiles` | Nutrition data for result cards |
| `gold.recipe_ingredients` + `gold.ingredients` | SQL fallback filter search |
| `gold.b2c_customer_dietary_preferences` | Graph personalization context |
| `gold.b2c_customer_allergens` | Graph allergen safety check |

### 10.3.4 Frontend Changes

| File | Change |
|------|--------|
| `app/page.tsx` or `app/search/page.tsx` | Add NL search hint text below search bar |
| **[NEW]** `components/search/match-reason-badge.tsx` | Sparkle icon + reason text badge |
| `components/search/recipe-card.tsx` (or equivalent) | Render `reasons[]` array as badges |
| Search results component | Show "Did you mean...?" when < 3 results |

**Match Reason Badge Component:**

```tsx
// components/search/match-reason-badge.tsx
import { Badge } from "@/components/ui/badge";
import { Sparkles } from "lucide-react";

export function MatchReasonBadge({ reason }: { reason: string }) {
  return (
    <Badge variant="outline" className="text-xs gap-1">
      <Sparkles className="w-3 h-3" />
      {reason}
    </Badge>
  );
}
```

**NL Search Hints:**

```tsx
<p className="text-muted-foreground text-sm mt-1">
  Try: "high protein vegan dinner under 30 minutes" or "kid-friendly lunch without nuts"
</p>
```

## 10.4 Acceptance Criteria

- [ ] NL query "vegan breakfast" returns relevant recipes (not just recipes with "vegan" in the title)
- [ ] Filter-only search (e.g., cuisine=Italian, calMax=500) still works via SQL
- [ ] Match reason badges display on results when RAG provides reasons
- [ ] When `USE_GRAPH_SEARCH=false`, search falls back to SQL with identical UI
- [ ] When RAG API is down, search falls back to SQL within 60s timeout (testing)
- [ ] Search results maintain fast response time
- [ ] NL search hints appear below search bar

---

## 10.RAG — RAG Team Scope

> **Repo:** `rag-pipeline-hybrid-reterival`  
> **Owner:** RAG Pipeline Engineer  
> **The B2C team does NOT touch these files.**

### Deliverables

#### 1. `POST /search/hybrid` Endpoint

Implement hybrid search combining semantic (vector similarity) + structural (graph) retrieval:

**Request:**

```json
{
  "query": "high protein vegan dinner under 30 minutes",
  "filters": { "diets": ["vegan"], "proteinMin": 25, "timeMax": 30 },
  "customer_id": "uuid"
}
```

**Response:**

```json
{
  "results": [
    {
      "id": "recipe-uuid",
      "score": 0.92,
      "reasons": ["Matches your Vegan diet", "32g protein per serving", "25 min total time"],
      "match_type": "semantic+structural"
    }
  ],
  "query_interpretation": "Looking for high-protein vegan dinner recipes with max 30 min cooking time"
}
```

#### 2. Pipeline Integration

- Use existing `orchestrator.py` pipeline (NLU → semantic → structural → Cypher)
- The `find_recipe` and `find_recipe_by_pantry` intents should trigger full pipeline
- Filter-only queries (no NL text) can skip semantic step and go straight to structural
- Return `reasons[]` array per result explaining why each recipe matched

#### 3. Cypher Queries

- Diet compliance check: traverse `Customer→[:FOLLOWS_DIET]→Diet→[:DIET_ALLOWS]→Recipe`
- Allergen safety: traverse `Customer→[:ALLERGIC_TO]→Allergen→[:CONTAINS_ALLERGEN]→Recipe` (exclude matches)
- Graph scoring: combine semantic similarity score + structural match signals

## 10.5 Route Registration

No new routes — modifies existing search endpoint in `server/routes/recipes.ts`.

## 10.6 Environment Variables

```env
# Already defined in PRD-09:
USE_GRAPH_SEARCH=false  # Set to 'true' to enable graph-enhanced search
```
