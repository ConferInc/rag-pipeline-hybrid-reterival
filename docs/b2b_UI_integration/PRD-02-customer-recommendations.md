# PRD-02: Customer Recommendations

> **Tech Stack:** Next.js (frontend), Express + Drizzle ORM (backend), FastAPI (RAG API), Neo4j 5 (graph DB), PostgreSQL `gold` schema
> **Auth:** Appwrite (B2B auth) → Express validates JWT → Express calls RAG API with `X-API-Key`
> **Repos:** `nutrib2b-v20` (frontend), `nutriapp-backend` (backend), `rag-pipeline-hybrid-reterival` (RAG API)
> **Depends On:** PRD-01 (Foundation & RAG Infra)

---

## 2.1 Overview

On the **Customer Detail page** (`/customers/[id]`), after viewing a B2B customer's profile (allergens, health conditions, dietary preferences, health metrics), the vendor user can click "Get Recommendations" to receive a ranked list of **vendor products** that are safe and nutritionally aligned for that customer.

**Why this matters:** Currently there is no way for a vendor admin to automatically find which products from their catalog would be a good match for a specific customer. They must manually cross-reference allergens, health conditions, and dietary preferences — a time-consuming and error-prone process.

**Current State:**

- Backend: Customer data routes exist (`GET /customers/:id`) returning full profile with allergens, conditions, diets, and health metrics. No recommendation endpoint.
- Frontend: Customer detail page exists at `app/customers/[id]/page.tsx` with profile, health, allergen, condition, and diet sections. No recommendation section.
- RAG Pipeline: Profile enrichment module (`profile_enrichment.py`) already merges health profiles into graph entities. Scoring infrastructure exists.

**SQL Fallback:** If `USE_GRAPH_RECOMMEND=false` or RAG API is down → recommendations section shows "Recommendations unavailable" message. No SQL-only fallback for recommendations since this is inherently a graph-powered feature.

## 2.2 User Stories

| ID | Story | Priority |
|----|-------|----------|
| CR-1 | As a vendor admin, I click "Get Recommendations" on a customer page and see a ranked product list | P0 |
| CR-2 | As a vendor admin, each recommended product shows match reasons (e.g., "No allergen conflicts", "High protein matches diet") | P0 |
| CR-3 | As a vendor admin, products with allergen conflicts are NEVER recommended | P0 |
| CR-4 | As a vendor admin, I can filter recommendations by category, nutrition range, or price | P1 |
| CR-5 | As a vendor admin, I see an LLM-generated summary explaining the recommendation strategy | P1 |
| CR-6 | As a vendor admin, recommendations work even if the RAG API is down (graceful message) | P0 |

## 2.3 Technical Architecture

### 2.3.1 Backend API

#### [NEW] `server/routes/recommendations.ts`

```typescript
import { Router } from "express";
import { requireAuth } from "../middleware/auth.js";
import { ragRecommend } from "../services/ragClient.js";

const router = Router();

// GET /api/v1/customers/:id/recommendations
router.get("/customers/:id/recommendations", requireAuth, async (req, res) => {
  const { id } = req.params;
  const vendorId = req.vendorId;
  const { limit = "20", category, minProtein, maxCalories } = req.query;

  // 1. Load customer profile from Supabase
  const [allergens, conditions, diets, healthProfile] = await Promise.all([
    storage.getB2BCustomerAllergens(id, vendorId),
    storage.getB2BCustomerConditions(id, vendorId),
    storage.getB2BCustomerDiets(id, vendorId),
    storage.getB2BCustomerHealthProfile(id, vendorId),
  ]);

  // 2. Call RAG pipeline
  const ragResult = await ragRecommend({
    b2b_customer_id: id,
    vendor_id: vendorId,
    allergens: allergens.map(a => a.code),
    health_conditions: conditions.map(c => c.code),
    dietary_preferences: diets.map(d => d.code),
    health_profile: healthProfile,
    limit: parseInt(limit as string),
    filters: { category, minProtein, maxCalories },
  });

  if (!ragResult) {
    return res.json({
      products: [],
      explanation: null,
      fallback: true,
      message: "Recommendation engine is currently unavailable",
    });
  }

  res.json(ragResult);
});

export default router;
```

#### [MODIFY] `server/routes.ts`

```typescript
import recommendations from "./routes/recommendations.js";
app.use("/api/v1", recommendations);
```

### 2.3.2 Neo4j Cypher Query Pattern

```cypher
// Step 1: Get customer profile from graph
MATCH (c:B2BCustomer {id: $customer_id})-[:BELONGS_TO_VENDOR]->(v:Vendor {id: $vendor_id})
OPTIONAL MATCH (c)-[:ALLERGIC_TO]->(a:Allergen)
OPTIONAL MATCH (c)-[:HAS_CONDITION]->(hc:HealthCondition)
OPTIONAL MATCH (c)-[:FOLLOWS_DIET]->(dp:DietaryPreference)
OPTIONAL MATCH (c)-[:HAS_PROFILE]->(hp:B2BHealthProfile)

WITH c, v, hp,
     COLLECT(DISTINCT a.code) AS allergen_codes,
     COLLECT(DISTINCT hc.code) AS condition_codes,
     COLLECT(DISTINCT dp.code) AS diet_codes

// Step 2: Find vendor products, exclude allergen-containing
MATCH (p:Product)-[:SOLD_BY]->(v)
WHERE p.status = 'active'
AND NOT EXISTS {
  MATCH (p)-[:CONTAINS_INGREDIENT]->(i:Ingredient)-[:CONTAINS_ALLERGEN]->(al:Allergen)
  WHERE al.code IN allergen_codes
}

// Step 3: Score by nutritional alignment
WITH p, hp,
     CASE WHEN hp.target_calories IS NOT NULL
          THEN 1.0 - ABS(p.calories - hp.target_calories) / COALESCE(hp.target_calories, 1)
          ELSE 0.5 END AS calorie_score,
     CASE WHEN hp.target_protein_g IS NOT NULL AND p.protein_g IS NOT NULL
          THEN CASE WHEN p.protein_g >= hp.target_protein_g * 0.1 THEN 1.0 ELSE 0.3 END
          ELSE 0.5 END AS protein_score

RETURN p.id, p.name, p.brand, p.calories, p.protein_g, p.image_url,
       (calorie_score + protein_score) / 2.0 AS score
ORDER BY score DESC
LIMIT $limit
```

### 2.3.3 Database Tables Used (Already Exist)

| Table | Usage |
|-------|-------|
| `gold.b2b_customers` | Customer identity + vendor scoping |
| `gold.b2b_customer_allergens` | Allergen exclusions (with severity) |
| `gold.b2b_customer_health_conditions` | Health condition context |
| `gold.b2b_customer_dietary_preferences` | Diet alignment scoring |
| `gold.b2b_customer_health_profiles` | Nutrition target values (TDEE, BMR, etc.) |
| `gold.products` | Product catalog for vendor |
| `gold.product_ingredients` + `gold.ingredients` | Ingredient-level data |
| `gold.ingredient_allergens` | Allergen detection per ingredient |

### 2.3.4 Frontend Changes

#### [MODIFY] `app/customers/[id]/page.tsx`

Add a new "Recommended Products" section/tab:

```
Customer Detail Page (Enhanced)
├── Profile Info (existing)
├── Health Profile (existing)
├── Allergens (existing)
├── Health Conditions (existing)
├── Dietary Preferences (existing)
└── 🆕 Recommended Products Tab
    ├── "Get Recommendations" button → triggers API call
    ├── Loading skeleton while fetching
    ├── Product cards with:
    │   ├── Product name, brand, image
    │   ├── Score badge (0-100%)
    │   ├── Match reason badges (✅ "No allergen conflicts", 🎯 "High protein")
    │   └── "Why recommended?" expandable section
    ├── Filters bar (category, nutrition range)
    ├── Empty state: "No matching products found"
    └── Fallback state: "Recommendation engine unavailable"
```

#### [NEW] `components/recommendations/RecommendationCard.tsx`

```tsx
interface RecommendationCardProps {
  product: {
    id: string;
    name: string;
    brand: string;
    score: number;
    reasons: string[];
    calories?: number;
    protein_g?: number;
    image_url?: string;
  };
}
```

---

## 2.RAG — RAG Team Scope

> **Repo:** `rag-pipeline-hybrid-reterival`
> **Owner:** RAG Pipeline Engineer
> **The B2B team does NOT touch these files.**

### Deliverables

#### 1. `POST /b2b/recommend-products` Endpoint

Implement vendor-scoped product recommendation using graph traversal + scoring.

**Request:**

```json
{
  "b2b_customer_id": "uuid",
  "vendor_id": "uuid",
  "allergens": ["peanut", "gluten"],
  "health_conditions": ["diabetes"],
  "dietary_preferences": ["keto"],
  "health_profile": {
    "target_calories": 2000,
    "target_protein_g": 120,
    "bmi": 25.5
  },
  "limit": 20,
  "filters": { "category": "snacks", "maxCalories": 300 }
}
```

**Response:**

```json
{
  "products": [
    {
      "id": "product-uuid",
      "name": "Almond Protein Bar",
      "brand": "NutriCo",
      "score": 0.92,
      "reasons": ["No allergen conflicts", "32g protein (meets target)", "Keto-compatible"],
      "calories": 280,
      "protein_g": 32,
      "image_url": "https://..."
    }
  ],
  "explanation": "Selected 15 products from your catalog that are allergen-safe and nutritionally aligned for this customer's keto diet and high-protein targets.",
  "retrieval_time_ms": 450
}
```

#### 2. Recommendation Scoring Algorithm

Combine multiple scoring signals:

| Signal | Weight | Method |
|--------|--------|--------|
| Allergen safety | Hard filter | Exclude products with matching allergens |
| Health condition compatibility | Hard filter | Exclude restricted ingredients |
| Nutrition alignment | 0.4 | Distance to target macros |
| Diet compatibility | 0.3 | Graph traversal: Product → Diet match |
| Quality score | 0.2 | Use ProductQualityScore if available |
| Interaction history | 0.1 | Boost previously viewed/purchased |

#### 3. Profile Enrichment

Use existing `profile_enrichment.py` to merge customer health profiles into the GraphRAG entity context, ensuring the LLM explanation has full profile context.

---

## 2.4 Acceptance Criteria

- [ ] `GET /api/v1/customers/:id/recommendations` returns ranked product list
- [ ] Products with allergen conflicts are NEVER included (hard filter)
- [ ] Each product has a `score` (0-1) and `reasons[]` array
- [ ] When `USE_GRAPH_RECOMMEND=false`, endpoint returns `{ fallback: true, message: "..." }`
- [ ] Frontend renders recommendation cards with scores and reason badges
- [ ] Recommendations are scoped to vendor's products only
- [ ] Response time < 5 seconds under normal load

## 2.5 Route Registration

```typescript
// In server/routes.ts
import recommendations from "./routes/recommendations.js";
app.use("/api/v1", recommendations);
```

## 2.6 Environment Variables

```env
# Already defined in PRD-01:
USE_GRAPH_RECOMMEND=false  # Set to 'true' to enable recommendations
```
