# PRD 18: Smart Substitutions

> **Tech Stack:** Next.js (frontend), Express + Drizzle ORM (backend), FastAPI (RAG API), Neo4j 5 (graph DB), PostgreSQL `gold` schema  
> **Auth:** Appwrite (B2C auth)  
> **Repos:** `nutrib2c-frontend` (frontend), `nutrition-backend-b2c` (backend), `rag-pipeline-hybrid-reterival` (RAG API)  
> **Family Context:** All features support household/family member switching. The `households` table is the root entity; each member is a `b2c_customers` row linked via `household_id`.  
> **Depends On:** PRD-09 (Foundation & Resilience), PRD-13 (Grocery — product substitutions), PRD-14 (Scanner — product alternatives)

---

## 18.1 Overview

Provide intelligent ingredient and product substitutions powered by graph `[:CAN_SUBSTITUTE]` edges. This spans three surfaces: (1) recipe-level ingredient swaps ("Can I use almond flour instead of wheat flour?"), (2) grocery list product swaps (cheaper or allergen-safe alternatives), and (3) scanner product alternatives. The graph traverses substitution relationships enriched with reasons (allergen safety, price savings, nutritional equivalence).

**Why this matters:** Users frequently need to substitute ingredients due to allergens, budget, or availability. Currently, the app doesn't offer any substitution intelligence — users have to figure it out themselves. The graph's `[:CAN_SUBSTITUTE]` edges (both Ingredient→Ingredient and Product→Product) enable targeted suggestions with explanations: "Use coconut aminos instead of soy sauce — soy-free and similar umami flavor."

**Current State:**

- Backend: `groceryList.ts` has a `getSubstitutionCandidates()` function but it only does SQL category-based product matching (same category, different brand). No ingredient-level substitutions.
- Frontend: No substitution UI exists anywhere.
- Neo4j: `[:CAN_SUBSTITUTE]` relationships will be created by the PG→Neo4j sync (PRD-09), initially seeded from curated data.

**SQL Fallback:** When graph is unavailable → ingredient substitutions return empty (feature not available). Product substitutions in grocery/scanner are already handled by PRD-13 and PRD-14's individual fallback logic.

## 18.2 User Stories

| ID | Story | Priority |
|----|-------|----------|
| SS-1 | As a user viewing a recipe, I tap an ingredient and see substitution options | P0 |
| SS-2 | As a user, each substitution shows a reason ("Gluten-free", "Cheaper", "Similar flavor") | P0 |
| SS-3 | As a user, substitutions respect my allergen profile | P0 |
| SS-4 | As a user, I see price savings when a cheaper substitute is available | P1 |
| SS-5 | As a user, I see nutritional comparison between original and substitute | P1 |
| SS-6 | As a user, substitutions work in recipe view, grocery list, and scanner | P0 |

## 18.3 Technical Architecture

### 18.3.1 Backend API

#### [NEW] `server/routes/substitutions.ts`

```typescript
import { ragProducts } from "./ragClient.js";

// GET /api/v1/substitutions/ingredient/:ingredientId
router.get("/ingredient/:ingredientId", requireAuth, async (req, res) => {
  const { ingredientId } = req.params;
  const memberId = req.query.memberId as string;

  // Get member allergens for safety filtering
  const allergens = await getMemberAllergenIds(memberId || req.user.id);

  // Try graph substitutions
  const graphSubs = await callRag("grocery", "/substitutions/ingredient", {
    ingredient_id: ingredientId,
    customer_allergens: allergens,
  });

  if (graphSubs) {
    res.json({ substitutions: graphSubs.substitutions });
    return;
  }

  // SQL fallback: no ingredient-level substitutions
  res.json({ substitutions: [] });
});

// GET /api/v1/substitutions/product/:productId
router.get("/product/:productId", requireAuth, async (req, res) => {
  const { productId } = req.params;
  const memberId = req.query.memberId as string;
  const allergens = await getMemberAllergenIds(memberId || req.user.id);

  const graphSubs = await ragAlternatives(productId, allergens);

  if (graphSubs) {
    res.json({ substitutions: graphSubs.alternatives });
    return;
  }

  res.json({ substitutions: [] });
});
```

### 18.3.2 RAG API Endpoint

`POST /substitutions/ingredient` (new endpoint in RAG API):

```json
// Request
{
  "ingredient_id": "uuid",
  "customer_allergens": ["uuid-gluten"]
}

// Response
{
  "substitutions": [
    {
      "ingredient_id": "alt-uuid",
      "name": "Almond Flour",
      "reason": "Gluten-free alternative to wheat flour",
      "category": "Baking",
      "nutritionComparison": {
        "original": { "calories_per_100g": 364, "protein_g": 10.3 },
        "substitute": { "calories_per_100g": 571, "protein_g": 21.2 }
      },
      "allergenSafe": true,
      "confidence": 0.85
    }
  ]
}
```

### 18.3.3 Graph Traversal

```cypher
// Ingredient-level substitutions
MATCH (original:Ingredient {id: $ingredientId})-[sub:CAN_SUBSTITUTE]->(alt:Ingredient)
WHERE NOT EXISTS {
  MATCH (alt)-[:IS_ALLERGEN]->(a:Allergen)
  WHERE a.id IN $allergenIds
}
RETURN alt.id, alt.name, sub.reason,
       alt.calories_per_100g, alt.protein_g,
       original.calories_per_100g AS orig_cal, original.protein_g AS orig_prot
ORDER BY sub.confidence DESC
LIMIT 5

// Product-level substitutions (reuses PRD-13/14 graph query)
MATCH (original:Product {id: $productId})-[sub:CAN_SUBSTITUTE]->(alt:Product)
WHERE NOT EXISTS {
  MATCH (alt)-[:CONTAINS_INGREDIENT]->(:Ingredient)-[:IS_ALLERGEN]->(a:Allergen)
  WHERE a.id IN $allergenIds
}
RETURN alt.id, alt.name, alt.brand, alt.price,
       sub.reason, sub.savings,
       original.price - alt.price AS computed_savings
ORDER BY computed_savings DESC
LIMIT 3
```

### 18.3.4 Substitution Data Seeding

`[:CAN_SUBSTITUTE]` edges need initial data. Strategy:

1. **Curated seed data:** Create a CSV of ~200 common substitutions (e.g., butter→olive oil, soy sauce→coconut aminos) with reasons
2. **LLM-generated:** For ingredients without curated subs, use LLM to suggest and create edges (with lower confidence scores)
3. **User feedback loop (Phase 2):** Let users accept/reject suggestions to improve the graph

### 18.3.5 Database Tables Used (Already Exist)

| Table | Usage |
|-------|-------|
| `gold.ingredients` | Ingredient details for display |
| `gold.products` | Product details for display |
| `gold.b2c_customer_allergens` | Allergen safety filtering |

### 18.3.6 Frontend Changes

| File | Change |
|------|--------|
| **[NEW]** `components/recipe/ingredient-substitution.tsx` | Pop-over/drawer showing ingredient substitutions |
| **[NEW]** `components/shared/substitution-card.tsx` | Reusable card: name, reason, savings, nutrition comparison |
| **[NEW]** `components/shared/nutrition-comparison.tsx` | Side-by-side nutrition comparison mini table |
| Recipe detail/analyzer ingredient list | Add "Swap" icon button next to each ingredient |
| `lib/substitution-api.ts` | API client for substitution endpoints |

**Ingredient Substitution Pop-over:**

```tsx
// components/recipe/ingredient-substitution.tsx
export function IngredientSubstitution({ ingredientId, memberId }: Props) {
  const { data } = useQuery(["subs", ingredientId], () =>
    fetchSubstitutions(ingredientId, memberId)
  );

  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button variant="ghost" size="sm">
          <ArrowRightLeft className="w-3 h-3" />
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-80">
        <h4 className="font-medium text-sm mb-2">Substitutions</h4>
        {data?.substitutions.length === 0 ? (
          <p className="text-xs text-muted-foreground">No substitutions available</p>
        ) : (
          <div className="space-y-2">
            {data?.substitutions.map(sub => (
              <SubstitutionCard key={sub.ingredient_id} sub={sub} />
            ))}
          </div>
        )}
      </PopoverContent>
    </Popover>
  );
}
```

**Nutrition Comparison:**

```tsx
// components/shared/nutrition-comparison.tsx
export function NutritionComparison({ original, substitute }: Props) {
  return (
    <div className="grid grid-cols-3 text-xs gap-1 mt-1">
      <div />
      <div className="text-center font-medium">Original</div>
      <div className="text-center font-medium">Substitute</div>
      
      <div>Calories</div>
      <div className="text-center">{original.calories_per_100g}</div>
      <div className="text-center">{substitute.calories_per_100g}</div>
      
      <div>Protein</div>
      <div className="text-center">{original.protein_g}g</div>
      <div className="text-center">{substitute.protein_g}g</div>
    </div>
  );
}
```

## 18.4 Acceptance Criteria

- [ ] Tapping an ingredient in recipe view shows substitution pop-over
- [ ] Each substitution has a reason explainer
- [ ] Substitutions are allergen-safe for the selected member
- [ ] Nutrition comparison table renders for ingredient substitutions
- [ ] Product substitutions (grocery/scanner) show savings when applicable
- [ ] When graph is down, substitution endpoints return empty array (not errors)
- [ ] `[:CAN_SUBSTITUTE]` edges seeded with ≥ 200 curated ingredient pairs
- [ ] Substitution pop-over opens within 60s (testing timeout)

---

## 18.RAG — RAG Team Scope

> **Repo:** `rag-pipeline-hybrid-reterival`  
> **Owner:** RAG Pipeline Engineer  
> **The B2C team does NOT touch these files.**

### Deliverables

#### 1. `POST /substitutions/ingredient` Endpoint (NEW)

Find allergen-safe ingredient substitutions with nutrition comparison:

**Request:**

```json
{
  "ingredient_id": "uuid",
  "customer_allergens": ["uuid-gluten"]
}
```

**Response:**

```json
{
  "substitutions": [
    {
      "ingredient_id": "alt-uuid",
      "name": "Almond Flour",
      "reason": "Gluten-free alternative to wheat flour",
      "category": "Baking",
      "nutritionComparison": {
        "original": { "calories_per_100g": 364, "protein_g": 10.3 },
        "substitute": { "calories_per_100g": 571, "protein_g": 21.2 }
      },
      "allergenSafe": true,
      "confidence": 0.85
    }
  ]
}
```

#### 2. Cypher for Ingredient Substitutions

```cypher
MATCH (original:Ingredient {id: $ingredientId})-[sub:CAN_SUBSTITUTE]->(alt:Ingredient)
WHERE NOT EXISTS {
  MATCH (alt)-[:IS_ALLERGEN]->(a:Allergen)
  WHERE a.id IN $allergenIds
}
RETURN alt.id, alt.name, sub.reason,
       alt.calories_per_100g, alt.protein_g,
       original.calories_per_100g AS orig_cal, original.protein_g AS orig_prot
ORDER BY sub.confidence DESC
LIMIT 5
```

#### 3. `[:CAN_SUBSTITUTE]` Seed Data

Create a CSV of ~200 common ingredient substitutions with:

- `original_ingredient_id`, `substitute_ingredient_id`
- `reason` (human-readable: "Gluten-free alternative", "Lower calorie option")
- `confidence` (0–1, curated = 0.9+, LLM-generated = 0.6–0.8)

Load via MERGE script as part of PG→Neo4j sync.

## 18.5 Route Registration

Add to `server/routes.ts`:

```typescript
import substitutionRouter from "./routes/substitutions.js";
app.use("/api/v1/substitutions", substitutionRouter);
```

## 18.6 Environment Variables

No new environment variables — uses existing RAG API connection and feature flags from PRD-09.
