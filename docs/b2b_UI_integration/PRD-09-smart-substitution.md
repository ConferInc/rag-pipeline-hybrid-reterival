# PRD-09: Smart Substitution Finder

> **Tech Stack:** Next.js (frontend), Express + Drizzle ORM (backend), FastAPI (RAG API), Neo4j 5 (graph DB), PostgreSQL `gold` schema
> **Auth:** Appwrite (B2B auth) → Express validates JWT → Express calls RAG API with `X-API-Key`
> **Repos:** `nutrib2b-v20` (frontend), `nutriapp-backend` (backend), `rag-pipeline-hybrid-reterival` (RAG API)
> **Depends On:** PRD-01 (Foundation & RAG Infra)

---

## 9.1 Overview

Given a product, dynamically find **substitution alternatives** from the vendor's catalog using Knowledge Graph-based scoring — no pre-populated substitution data needed.

> **Critical Context:** The `product_substitutions` table exists in `gold.sql` but is **NOT being ingested** (empty). The `SUBSTITUTE_FOR` relationship in Neo4j is also **NOT populated** per `REMAINING_INTENTS.md`. This PRD uses a dynamic graph-computation approach instead.

**Why this approach is better than static tables:**

1. **Dynamic** — auto-updates when products are added/removed
2. **Personalized** — can consider a specific customer's health profile
3. **Vendor-scoped** — only suggests products in the vendor's catalog
4. **Multi-signal** — combines category, nutrition, ingredient overlap, and allergen safety

## 9.2 User Stories

| ID | Story | Priority |
|----|-------|----------|
| SF-1 | As a vendor admin, I click "Find Substitutes" on a product and see alternatives | P0 |
| SF-2 | As a vendor admin, substitutes are scored by nutrition similarity and ingredient overlap | P0 |
| SF-3 | As a vendor admin, I can optionally specify a customer to find health-safe alternatives | P1 |
| SF-4 | As a vendor admin, each substitute shows why it was selected (reasons) | P0 |
| SF-5 | As a vendor admin, I see LLM-generated reasoning for the top substitute (e.g., "Similar nutrition, no peanuts") | P1 |

## 9.3 Technical Architecture

### 9.3.1 Backend API

#### [MODIFY] `server/routes/recommendations.ts` (extend existing)

```typescript
// POST /api/v1/products/:id/substitutions
router.post("/products/:id/substitutions", requireAuth, async (req, res) => {
  const { id } = req.params;
  const vendorId = req.vendorId;
  const { customer_id, limit = 10 } = req.body;

  const ragResult = await ragSubstitutions({
    product_id: id,
    vendor_id: vendorId,
    customer_id: customer_id || null,
    limit,
  });

  if (!ragResult) {
    return res.json({
      substitutes: [],
      fallback: true,
      message: "Substitution finder unavailable",
    });
  }

  res.json(ragResult);
});
```

### 9.3.2 Four Substitution Strategies (Combined Scoring)

**Strategy 1: Category + Nutrition Similarity (weight: 0.3)**

Score candidates in the same category by how close their nutrition profile is to the original.

**Strategy 2: Ingredient Overlap — Jaccard Index (weight: 0.3)**

Measure ingredient similarity between products using Jaccard coefficient: `|intersection| / |union|`.

**Strategy 3: Health-Profile-Aware Substitution (weight: 0.3 when customer_id provided)**

When a customer is specified:

1. Get customer allergens from graph
2. Find same-category candidates from same vendor
3. EXCLUDE candidates with matching allergens
4. RANK by nutrition similarity

**Strategy 4: LLM-Generated Reasoning (weight: 0.1)**

Pass top candidates through LLM to generate human-readable explanation.

### 9.3.3 Frontend Changes

#### [MODIFY] `app/products/page.tsx`

```
Product Row Actions (Enhanced)
├── View Details (existing)
├── Edit (existing)
├── Find Matching Customers (PRD-04)
└── 🆕 "Find Substitutes" button
    └── Opens subsitution panel:
        ├── Original product summary (name, brand, category, nutrition)
        ├── Optional: Customer selector (dropdown — for health-aware substitution)
        ├── Substitute list:
        │   ├── Each substitute card:
        │   │   ├── Product name, brand
        │   │   ├── Similarity score (0-100%)
        │   │   ├── Score breakdown: Category ✅, Nutrition 87%, Ingredients 65%
        │   │   ├── Reason badges: "Same category", "Similar macros", "No peanuts"
        │   │   └── LLM reasoning tooltip (expandable)
        │   └── Sort by: Overall score (default), Nutrition match, Ingredient overlap
        ├── Empty state: "No suitable substitutes found in catalog"
        └── Fallback: "Substitution finder unavailable"
```

---

## 9.RAG — RAG Team Scope

> **Owner:** RAG Pipeline Engineer

### Deliverables

#### 1. `POST /b2b/substitutions` Endpoint

**Request:**

```json
{
  "product_id": "uuid",
  "vendor_id": "uuid",
  "customer_id": "uuid-or-null",
  "limit": 10
}
```

**Response:**

```json
{
  "original": {
    "id": "uuid",
    "name": "Peanut Butter Crackers",
    "brand": "SnackCo",
    "category": "Crackers",
    "calories": 200,
    "protein_g": 8
  },
  "substitutes": [
    {
      "id": "uuid2",
      "name": "Sunflower Seed Crackers",
      "brand": "HealthyCo",
      "score": 0.87,
      "score_breakdown": {
        "category_match": 1.0,
        "nutrition_similarity": 0.85,
        "ingredient_overlap": 0.65,
        "allergen_safety": 1.0
      },
      "reasons": ["Same category", "Similar calories (190 vs 200)", "No peanuts"],
      "llm_reasoning": "Sunflower Seed Crackers is a strong substitute — similar nutrition profile (190 vs 200 cal). Free from peanuts. Sunflower seeds provide similar protein content."
    }
  ],
  "customer_context": {
    "customer_id": "uuid",
    "allergens_excluded": ["peanut"],
    "note": "Excluded 3 products containing peanuts"
  }
}
```

#### 2. Category + Nutrition Similarity Cypher

```cypher
MATCH (original:Product {id: $product_id})-[:SOLD_BY]->(v:Vendor {id: $vendor_id})
MATCH (candidate:Product)-[:SOLD_BY]->(v)
WHERE candidate.id <> original.id AND candidate.status = 'active'
  AND candidate.category_id = original.category_id
WITH original, candidate,
     CASE WHEN original.calories IS NOT NULL AND candidate.calories IS NOT NULL
          THEN 1.0 - ABS(original.calories - candidate.calories) / COALESCE(NULLIF(original.calories,0), 1)
          ELSE 0.5 END AS calorie_sim,
     CASE WHEN original.protein_g IS NOT NULL AND candidate.protein_g IS NOT NULL
          THEN 1.0 - ABS(original.protein_g - candidate.protein_g) / COALESCE(NULLIF(original.protein_g,0), 1)
          ELSE 0.5 END AS protein_sim,
     CASE WHEN original.total_fat_g IS NOT NULL AND candidate.total_fat_g IS NOT NULL
          THEN 1.0 - ABS(original.total_fat_g - candidate.total_fat_g) / COALESCE(NULLIF(original.total_fat_g,0), 1)
          ELSE 0.5 END AS fat_sim
WITH candidate, (calorie_sim + protein_sim + fat_sim) / 3.0 AS nutrition_score
WHERE nutrition_score > 0.4
RETURN candidate.id, candidate.name, candidate.brand, 
       candidate.calories, candidate.protein_g,
       round(nutrition_score, 2) AS score
ORDER BY nutrition_score DESC LIMIT $limit
```

#### 3. Health-Aware Substitution Cypher

```cypher
MATCH (original:Product {id: $product_id})-[:SOLD_BY]->(v:Vendor {id: $vendor_id})
MATCH (c:B2BCustomer {id: $customer_id})-[:ALLERGIC_TO]->(ca:Allergen)
WITH original, v, COLLECT(DISTINCT ca.code) AS customer_allergen_codes
MATCH (candidate:Product)-[:SOLD_BY]->(v)
WHERE candidate.id <> original.id AND candidate.status = 'active'
  AND candidate.category_id = original.category_id
  AND NOT EXISTS {
    MATCH (candidate)-[:CONTAINS_INGREDIENT]->(i)-[:CONTAINS_ALLERGEN]->(a:Allergen)
    WHERE a.code IN customer_allergen_codes
  }
RETURN candidate.id, candidate.name, candidate.brand, candidate.calories
ORDER BY ABS(COALESCE(candidate.calories,0) - COALESCE(original.calories,0)) ASC
LIMIT $limit
```

#### 4. Ingredient Jaccard Similarity Cypher

```cypher
MATCH (original:Product {id: $product_id})-[:CONTAINS_INGREDIENT]->(oi:Ingredient)
WITH original, COLLECT(oi) AS orig_ingredients, COUNT(oi) AS orig_count
MATCH (candidate:Product)-[:SOLD_BY]->(v:Vendor {id: $vendor_id})
WHERE candidate.id <> original.id AND candidate.status = 'active'
MATCH (candidate)-[:CONTAINS_INGREDIENT]->(ci:Ingredient)
WITH candidate, original, orig_ingredients, orig_count,
     COLLECT(ci) AS cand_ingredients, COUNT(ci) AS cand_count
WITH candidate,
     SIZE([i IN cand_ingredients WHERE i IN orig_ingredients]) AS overlap,
     orig_count, cand_count
WITH candidate,
     CASE WHEN (orig_count + cand_count - overlap) = 0 THEN 0
          ELSE toFloat(overlap) / (orig_count + cand_count - overlap)
     END AS jaccard
WHERE jaccard > 0.2
RETURN candidate.id, candidate.name, round(jaccard, 2) AS ingredient_similarity
ORDER BY jaccard DESC LIMIT $limit
```

---

## 9.4 Acceptance Criteria

- [ ] `POST /api/v1/products/:id/substitutions` returns ranked substitute list
- [ ] Substitutes are scored by combined signals (category + nutrition + ingredients)
- [ ] When customer_id provided, allergen-unsafe products are excluded
- [ ] Each substitute has `score`, `score_breakdown`, `reasons[]`
- [ ] LLM reasoning shows for top substitutes when RAG available
- [ ] Frontend renders substitution panel with score breakdowns
- [ ] Only products from the same vendor are suggested

## 9.5 Route Registration

```typescript
// Reuses recommendations.ts router from PRD-02
```

## 9.6 Environment Variables

```env
USE_GRAPH_SUBSTITUTE=false  # Set to 'true' to enable smart substitution
```
