# PRD-03: Graph-Enhanced Search

> **Tech Stack:** Next.js (frontend), Express + Drizzle ORM (backend), FastAPI (RAG API), Neo4j 5 (graph DB), PostgreSQL `gold` schema
> **Auth:** Appwrite (B2B auth) → Express validates JWT → Express calls RAG API with `X-API-Key`
> **Repos:** `nutrib2b-v20` (frontend), `nutriapp-backend` (backend), `rag-pipeline-hybrid-reterival` (RAG API)
> **Depends On:** PRD-01 (Foundation & RAG Infra)

---

## 3.1 Overview

Upgrade product search from client-side keyword filtering to a hybrid system: when the vendor user types a natural language query (e.g., "high protein snacks for diabetic customers"), the RAG API performs semantic + structural graph search. When the user only uses filters (category dropdown, nutrition sliders), existing SQL handles it — it's faster for indexed columns.

This PRD also includes **"Did You Mean?" query suggestions** and **health-context refinement** — when a user's search query contains health-related terms, the system suggests refined alternatives based on NLU entity extraction.

**Why this matters:** The current search in `search/page.tsx` fetches ALL products/customers via `GET /products` and `GET /customers`, then filters client-side by keyword matching. It can't understand "sugar free options for diabetics" or "high protein keto snacks". The RAG pipeline's NLU can extract health entities and use graph structure for dramatically better results.

**Current State:**

- Frontend: Full search page with tabs (Products/Customers/Jobs), filter panel (category, status, tags, nutrition ranges), debounced search-as-you-type. All client-side.
- Backend: `GET /products` and `GET /customers` return full lists. No server-side search endpoint with NLP.
- RAG Pipeline: NLU layer (`extractor_classifier.py`) already does entity extraction (diet keywords, nutrient thresholds, health conditions). `_keyword_extract()` handles ambiguous queries.

**SQL Fallback:** If `USE_GRAPH_SEARCH=false` or RAG API is down → silently falls back to existing client-side filtering. UI unchanged, results are just keyword-matched.

## 3.2 User Stories

| ID | Story | Priority |
|----|-------|----------|
| GS-1 | As a vendor admin, I type "high protein snacks for diabetics" and get relevant products | P0 |
| GS-2 | As a vendor admin, each search result shows match reason badges ("Matches Diabetic diet", "25g protein") | P0 |
| GS-3 | As a vendor admin, I can still use traditional filter-based search (category, nutrition ranges, etc.) | P0 |
| GS-4 | As a vendor admin, search works normally even if the graph database is down | P0 |
| GS-5 | As a vendor admin, I see NL search hints below the search bar to guide me | P1 |
| GS-6 | As a vendor admin, when I type a health-related query like "sugar free", I see "Did You Mean?" suggestions below the search bar | P1 |
| GS-7 | As a vendor admin, "Did You Mean?" suggestions include health-context refinements like "Products safe for customers with Diabetes" | P1 |
| GS-8 | As a vendor admin, clicking a suggestion chip auto-fills the search and triggers a new search | P1 |
| GS-9 | As a vendor admin, when my NL search returns < 3 results, I see semantic suggestions | P2 |

## 3.3 Technical Architecture

### 3.3.1 Backend API

#### [NEW] `server/routes/search-enhanced.ts` (or modify in `recommendations.ts`)

```typescript
import { Router } from "express";
import { requireAuth } from "../middleware/auth.js";
import { ragSearch, ragSearchSuggest } from "../services/ragClient.js";

const router = Router();

// POST /api/v1/search/products — NLP-powered product search
router.post("/search/products", requireAuth, async (req, res) => {
  const { query, filters, limit = 20 } = req.body;
  const vendorId = req.vendorId;

  // Try RAG search first (if flag ON + circuit CLOSED)
  if (query) {
    const ragResult = await ragSearch({
      query,
      vendor_id: vendorId,
      filters,
      limit,
    });

    if (ragResult) {
      return res.json(ragResult);
    }
  }

  // SQL fallback: server-side keyword search (better than client-side)
  const products = await storage.searchProducts(vendorId, {
    keyword: query,
    ...filters,
    limit,
  });

  res.json({
    results: products.map(p => ({ ...p, score: null, reasons: [] })),
    query_interpretation: null,
    fallback: true,
  });
});

// GET /api/v1/search/suggestions — "Did You Mean?" query expansion
router.get("/search/suggestions", requireAuth, async (req, res) => {
  const q = req.query.q as string;
  if (!q || q.length < 3) return res.json({ suggestions: [] });

  const vendorId = req.vendorId;

  const ragResult = await ragSearchSuggest({
    query: q,
    vendor_id: vendorId,
  });

  if (!ragResult) {
    return res.json({ suggestions: [], fallback: true });
  }

  res.json(ragResult);
});

export default router;
```

#### [MODIFY] `server/routes.ts`

```typescript
import searchEnhanced from "./routes/search-enhanced.js";
app.use("/api/v1", searchEnhanced);
```

### 3.3.2 "Did You Mean?" — NLU Entity Extraction Flow

```
User types: "sugar free"

1. Frontend debounces (300ms) → calls GET /api/v1/search/suggestions?q=sugar+free
2. Backend proxies to RAG: POST /b2b/search-suggest { query: "sugar free", vendor_id }
3. RAG NLU extracts entities:
   {
     "diet": ["Sugar-Free"],
     "health_conditions": ["Diabetes"],
     "certification": ["Sugar-Free Certified"]
   }
4. RAG generates suggestions:
   [
     "Products compatible with Sugar-Free diet",
     "Products safe for customers with Diabetes",
     "Products with Sugar-Free certification",
     "Products with 0g added sugar"
   ]
5. Frontend renders suggestion chips below search bar
```

### 3.3.3 Search Results with Match Reasons

```
Search results for "high protein keto snacks"
Query interpretation: "Looking for high-protein, keto-compatible snack products"

┌───────────────────────────────────────────────────────────┐
│ 🔍 Almond Protein Bar — NutriCo              Score: 0.95 │
│ ✨ Keto-compatible  ✨ 32g protein  ✨ Snack category     │
│ 280 cal | 32g protein | 15g fat | 5g carbs               │
├───────────────────────────────────────────────────────────┤
│ 🔍 Coconut Protein Bites — HealthyChoice      Score: 0.88│
│ ✨ Keto-compatible  ✨ 18g protein  ✨ Low carb (3g)     │
│ 190 cal | 18g protein | 12g fat | 3g carbs               │
└───────────────────────────────────────────────────────────┘
```

### 3.3.4 Database Tables Used (Already Exist)

| Table | Usage |
|-------|-------|
| `gold.products` | Product catalog (vendor-scoped) |
| `gold.product_ingredients` + `gold.ingredients` | Ingredient-level data for graph |
| `gold.ingredient_allergens` | Allergen detection |
| `gold.product_categories` | Category filtering |
| `gold.b2b_customer_allergens` | Health-context personalization (optional) |
| `gold.b2b_customer_dietary_preferences` | Diet context for suggestions |

### 3.3.5 Frontend Changes

#### [MODIFY] `app/search/page.tsx`

Major enhancements:

```
Search Page (Enhanced)
├── Search Bar (natural language input)
│   ├── Placeholder: "Try: 'gluten free snacks for diabetics' or 'high protein under 200 cal'"
│   └── 🆕 "Did You Mean?" suggestion chips (appear on typing)
│       ├── Chip: "Products compatible with Sugar-Free diet" → onClick fills search
│       ├── Chip: "Products safe for customers with Diabetes"
│       ├── Chip: "Products with 0g added sugar"
│       └── Chip: "Products with Sugar-Free certification"
├── 🆕 Query Interpretation Banner
│   └── "Showing results for: high-protein keto-compatible snack products"
├── 🆕 Toggle: "Use AI Search" / "Classic Search" (respects feature flag)
├── Filter Panel (existing — category, status, tags, nutrition ranges)
├── 🆕 Results with Match Reasons
│   ├── Product cards with score badge + reason badges
│   ├── "Why this result?" tooltip per card
│   └── Sort by: Relevance (default), Name, Calories, Protein
├── Empty state: "No matching products. Try a different search or adjust filters."
└── NL Search Hints (below search bar when empty)
    └── "Try: 'high protein vegan snacks' or 'products safe for nut allergy'"
```

#### [NEW] `components/search/MatchReasonBadge.tsx`

```tsx
import { Sparkles } from "lucide-react";

export function MatchReasonBadge({ reason }: { reason: string }) {
  return (
    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full 
                     bg-emerald-50 text-emerald-700 text-xs font-medium">
      <Sparkles className="w-3 h-3" />
      {reason}
    </span>
  );
}
```

#### [NEW] `components/search/SearchSuggestions.tsx`

```tsx
interface SearchSuggestionsProps {
  suggestions: string[];
  onSelect: (suggestion: string) => void;
  loading: boolean;
}

export function SearchSuggestions({ suggestions, onSelect, loading }: SearchSuggestionsProps) {
  if (loading) return <div className="text-sm text-muted">Loading suggestions...</div>;
  if (suggestions.length === 0) return null;
  
  return (
    <div className="flex flex-wrap gap-2 mt-2 p-3 bg-blue-50 rounded-lg">
      <span className="text-sm text-blue-700 font-medium">🔍 Did you mean:</span>
      {suggestions.map((s, i) => (
        <button key={i} onClick={() => onSelect(s)}
                className="px-3 py-1 rounded-full bg-white border border-blue-200 
                           text-sm text-blue-800 hover:bg-blue-100 transition-colors">
          {s}
        </button>
      ))}
    </div>
  );
}
```

#### [NEW] `hooks/useSearchSuggestions.ts`

```typescript
import { useState, useEffect, useRef } from "react";
import { apiFetch } from "@/lib/api";

export function useSearchSuggestions(query: string, debounceMs = 400) {
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const timeoutRef = useRef<NodeJS.Timeout>();

  useEffect(() => {
    if (query.length < 3) { setSuggestions([]); return; }

    clearTimeout(timeoutRef.current);
    timeoutRef.current = setTimeout(async () => {
      setLoading(true);
      try {
        const data = await apiFetch(`/search/suggestions?q=${encodeURIComponent(query)}`);
        setSuggestions(data.suggestions || []);
      } catch { setSuggestions([]); }
      setLoading(false);
    }, debounceMs);

    return () => clearTimeout(timeoutRef.current);
  }, [query, debounceMs]);

  return { suggestions, loading };
}
```

---

## 3.RAG — RAG Team Scope

> **Repo:** `rag-pipeline-hybrid-reterival`
> **Owner:** RAG Pipeline Engineer
> **The B2B team does NOT touch these files.**

### Deliverables

#### 1. `POST /b2b/search` Endpoint

Implement hybrid search combining semantic (vector similarity) + structural (graph) retrieval, scoped to vendor.

**Request:**

```json
{
  "query": "high protein keto snacks under 300 calories",
  "vendor_id": "uuid",
  "filters": {
    "category": "snacks",
    "maxCalories": 300,
    "diets": ["keto"],
    "allergen_free": ["peanut"]
  },
  "limit": 20
}
```

**Response:**

```json
{
  "results": [
    {
      "id": "product-uuid",
      "name": "Almond Protein Bar",
      "brand": "NutriCo",
      "score": 0.95,
      "reasons": ["Keto-compatible", "32g protein per serving", "280 cal (under 300)"],
      "match_type": "semantic+structural"
    }
  ],
  "query_interpretation": "Looking for high-protein, keto-compatible snack products under 300 calories",
  "total_found": 15,
  "retrieval_time_ms": 320
}
```

#### 2. `POST /b2b/search-suggest` Endpoint — "Did You Mean?"

Extract health entities from partial/ambiguous queries and return refined suggestions.

**Request:**

```json
{
  "query": "sugar free",
  "vendor_id": "uuid"
}
```

**Response:**

```json
{
  "suggestions": [
    "Products compatible with Sugar-Free diet",
    "Products safe for customers with Diabetes",
    "Products with Sugar-Free certification",
    "Products with 0g added sugar"
  ],
  "entities_found": {
    "diet": ["Sugar-Free"],
    "health_conditions": ["Diabetes"],
    "certification": ["Sugar-Free"]
  }
}
```

#### 3. Pipeline Integration

- Use existing `orchestrator.py` pipeline (NLU → semantic → structural → Cypher)
- Create new B2B intent `find_b2b_product` that triggers vendor-scoped pipeline
- Entity extraction should handle: diets, allergens, health conditions, nutrient thresholds, categories, certifications
- Filter-only queries (no NL text) can skip semantic step → go straight to structural
- Return `reasons[]` array per result explaining WHY each product matched

#### 4. Cypher Queries for Search

**NLP Search:**

```cypher
// Example for "high protein keto snacks under 300 calories"
MATCH (p:Product)-[:SOLD_BY]->(v:Vendor {id: $vendor_id})
WHERE p.status = 'active'
  AND p.calories <= 300
  AND p.protein_g >= 20
OPTIONAL MATCH (p)-[:COMPATIBLE_WITH_DIET]->(d:DietaryPreference {code: 'keto'})
WITH p, d,
     CASE WHEN d IS NOT NULL THEN 1.0 ELSE 0.0 END AS diet_score,
     CASE WHEN p.protein_g > 25 THEN 1.0
          WHEN p.protein_g > 15 THEN 0.7 ELSE 0.3 END AS protein_score
RETURN p.id, p.name, p.brand, p.calories, p.protein_g,
       (diet_score * 0.4 + protein_score * 0.6) AS score
ORDER BY score DESC
LIMIT $limit
```

**Suggestion generation:**

```python
def suggest_refinements(query: str, vendor_id: str) -> list[str]:
    """Extract health entities and generate refined query suggestions."""
    nlu = extract_hybrid(query)
    suggestions = []
    
    if nlu.entities.get("diet"):
        for diet in nlu.entities["diet"]:
            suggestions.append(f"Products compatible with {diet} diet")
    if nlu.entities.get("health_conditions"):
        for cond in nlu.entities["health_conditions"]:
            suggestions.append(f"Products safe for customers with {cond}")
    if nlu.entities.get("nutrient_threshold"):
        for nutrient, thresh in nlu.entities["nutrient_threshold"].items():
            suggestions.append(f"Products with {thresh['op']} {thresh['value']}g {nutrient}")
    if nlu.entities.get("certification"):
        for cert in nlu.entities["certification"]:
            suggestions.append(f"Products with {cert} certification")
    
    return suggestions[:5]
```

---

## 3.4 Acceptance Criteria

- [ ] NL query "gluten free snacks" returns relevant products (not just exact title matches)
- [ ] Filter-only search (category=snacks, maxCalories=300) still works via SQL fallback
- [ ] Match reason badges display on results when RAG provides reasons
- [ ] When `USE_GRAPH_SEARCH=false`, search falls back to client-side keyword filter
- [ ] When RAG API is down, search degrades gracefully (existing experience preserved)
- [ ] "Did You Mean?" suggestions appear within 500ms of typing (debounced)
- [ ] Clicking a suggestion chip triggers a new search with that query
- [ ] NL search hints appear below search bar when empty
- [ ] Query interpretation banner shows for NLP searches
- [ ] Search results are vendor-scoped (no cross-vendor leakage)

## 3.5 Route Registration

```typescript
// In server/routes.ts
import searchEnhanced from "./routes/search-enhanced.js";
app.use("/api/v1", searchEnhanced);
```

## 3.6 Environment Variables

```env
# Already defined in PRD-01:
USE_GRAPH_SEARCH=false  # Set to 'true' to enable graph-enhanced search + suggestions
```
