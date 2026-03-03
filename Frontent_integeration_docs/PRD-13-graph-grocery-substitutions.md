# PRD 13: Graph-Enhanced Grocery List & Substitutions

> **Tech Stack:** Next.js (frontend), Express + Drizzle ORM (backend), FastAPI (RAG API), Neo4j 5 (graph DB), PostgreSQL `gold` schema  
> **Auth:** Appwrite (B2C auth)  
> **Repos:** `nutrib2c-frontend` (frontend), `nutrition-backend-b2c` (backend), `rag-pipeline-hybrid-reterival` (RAG API)  
> **Family Context:** All features support household/family member switching. The `households` table is the root entity; each member is a `b2c_customers` row linked via `household_id`.  
> **Depends On:** PRD-09 (Foundation & Resilience), PRD-12 (Meal Planning — grocery lists are generated from meal plans)

---

## 13.1 Overview

Enhance the grocery list feature with graph-powered product recommendations and allergen-safe substitutions. When generating a grocery list from a meal plan, the system uses the graph to: (1) find the best product match for each ingredient considering allergens and price, and (2) offer substitution suggestions for any item — cheaper alternatives, allergen-safe swaps, or healthier options.

**Why this matters:** The current `generateGroceryList()` in `groceryList.ts` matches ingredients to products via SQL joins (`ingredient_id → product_ingredients → products`), choosing the cheapest USD match. It can't traverse allergen relationships, doesn't consider what the user has bought before, and doesn't suggest substitutions. The graph adds `[:CAN_SUBSTITUTE]` relationship traversal for intelligent alternatives.

**Current State:**

- Backend: `server/services/groceryList.ts` (853 lines) — fully functional: `generateGroceryList()`, `aggregateIngredients()`, `fetchIngredientMappedCandidates()`, `chooseCheapestUsd()`, `updateGroceryListItem()`, `getSubstitutionCandidates()`. Substitution function exists but only uses SQL category match.
- Frontend: Grocery list UI exists with item management, check-off, status transitions.

**SQL Fallback:** If `USE_GRAPH_GROCERY=false` or RAG API is down → existing `fetchIngredientMappedCandidates()` for product matching. Substitutions endpoint returns empty array — user just doesn't see alternative suggestions.

## 13.2 User Stories

| ID | Story | Priority |
|----|-------|----------|
| GL-1 | As a user, my grocery list automatically matches products that are safe for my family's allergens | P0 |
| GL-2 | As a user, I see substitution suggestions (cheaper, healthier, allergen-safe) next to each item | P1 |
| GL-3 | As a user, I see estimated savings when a cheaper substitute is available | P1 |
| GL-4 | As a user, substitution reasons are explained ("Cheaper by $1.20", "Gluten-free alternative") | P1 |
| GL-5 | As a user, my grocery list generates correctly even if the recommendation engine is down | P0 |
| GL-6 | As a user, I can accept a substitution and it replaces the item in my list | P0 |

## 13.3 Technical Architecture

### 13.3.1 Backend API

#### [MODIFY] `server/services/groceryList.ts`

Enhance product matching with graph-aware recommendations:

```typescript
import { ragProducts } from "./ragClient.js";

async function matchProductsForBuckets(
  buckets: Map<string, AggregatedBucket>,
  allergens: string[]
): Promise<Map<string, ProductCandidate>> {
  const ingredientIds = [...buckets.values()].map(b => b.ingredientId);

  // Try graph-aware product matching first
  const graphProducts = await ragProducts(ingredientIds, allergens);

  if (graphProducts) {
    // Graph returned allergen-safe, budget-aware product matches
    const productMap = new Map<string, ProductCandidate>();
    for (const match of graphProducts.products) {
      productMap.set(match.ingredient_id, {
        id: match.product_id,
        name: match.product_name,
        brand: match.brand,
        price: match.price,
        currency: match.currency,
        package_weight_g: match.weight_g,
        category_name: match.category,
        image_url: match.image_url,
      });
    }
    return productMap;
  }

  // SQL fallback: existing ingredient→product mapping
  return fetchIngredientMappedCandidates(ingredientIds);
}
```

#### [NEW] Substitution endpoint in `server/routes/groceryList.ts`

```typescript
// GET /api/v1/grocery-lists/:id/substitutions/:itemId
router.get("/:id/substitutions/:itemId", requireAuth, async (req, res) => {
  const { id: listId, itemId } = req.params;
  const household = await getOrCreateHousehold(req.user.id);
  await requireListForHousehold(listId, household.id);
  const item = await requireItemForList(itemId, listId);

  // Try graph substitutions first
  const graphSubs = await ragAlternatives(item.productId, household.allergenIds);

  if (graphSubs) {
    res.json({
      substitutions: graphSubs.alternatives.map((alt: any) => ({
        productId: alt.product_id,
        name: alt.name,
        brand: alt.brand,
        price: alt.price,
        imageUrl: alt.image_url,
        reason: alt.reason,        // "Cheaper by $1.20" or "Gluten-free"
        savings: alt.savings,      // null or number
        confidence: alt.confidence, // 0-1
      })),
    });
    return;
  }

  // SQL fallback: return empty (no substitutions available)
  res.json({ substitutions: [] });
});
```

### 13.3.2 RAG API Endpoint

`POST /recommend/products`:

```json
// Request
{
  "ingredient_ids": ["uuid-1", "uuid-2", "uuid-3"],
  "customer_allergens": ["uuid-gluten", "uuid-dairy"]
}

// Response
{
  "products": [
    {
      "ingredient_id": "uuid-1",
      "product_id": "prod-uuid",
      "product_name": "Bob's Red Mill GF Flour",
      "brand": "Bob's Red Mill",
      "price": 5.99,
      "currency": "USD",
      "weight_g": 680,
      "category": "Baking",
      "image_url": "...",
      "match_reason": "Allergen-safe (gluten-free), best price in category"
    }
  ]
}
```

### 13.3.3 Graph Traversal for Substitutions

The RAG API traverses `[:CAN_SUBSTITUTE]` edges in Neo4j:

```cypher
// Find allergen-safe substitutes for a product
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

### 13.3.4 Database Tables Used (Already Exist)

| Table | Usage |
|-------|-------|
| `gold.shopping_lists` + `gold.shopping_list_items` | List storage |
| `gold.products` + `gold.product_ingredients` | SQL fallback product matching |
| `gold.ingredients` | Ingredient names for display |
| `gold.b2c_customer_allergens` | Allergen context for graph |

### 13.3.5 Frontend Changes

| File | Change |
|------|--------|
| Grocery list item component | Add "Alternatives" button/icon next to each item |
| **[NEW]** `components/grocery/substitution-card.tsx` | Card showing alternative with reason, savings |
| **[NEW]** `components/grocery/substitution-drawer.tsx` | Slide-up drawer listing 3 substitutions |
| Grocery list item component | Handle "Accept substitution" action |

**Substitution Card:**

```tsx
// components/grocery/substitution-card.tsx
export function SubstitutionCard({ sub, onAccept }: Props) {
  return (
    <Card className="p-3">
      <div className="flex items-center gap-3">
        <img src={sub.imageUrl} className="w-12 h-12 rounded" />
        <div className="flex-1">
          <p className="font-medium text-sm">{sub.name}</p>
          <p className="text-xs text-muted-foreground">{sub.brand}</p>
          <Badge variant="outline" className="text-xs mt-1">
            {sub.reason}
          </Badge>
          {sub.savings > 0 && (
            <Badge variant="secondary" className="text-xs ml-1">
              Save ${sub.savings.toFixed(2)}
            </Badge>
          )}
        </div>
        <Button size="sm" onClick={() => onAccept(sub.productId)}>
          Swap
        </Button>
      </div>
    </Card>
  );
}
```

## 13.4 Acceptance Criteria

- [ ] Grocery list generation uses graph-matched products when `USE_GRAPH_GROCERY=true`
- [ ] Products matched are allergen-safe for the household
- [ ] Substitution endpoint returns up to 3 alternatives per item
- [ ] Each substitution includes a reason and optional savings amount
- [ ] Accepting a substitution updates the list item's product
- [ ] When RAG is down, product matching falls back to SQL
- [ ] When RAG is down, substitution endpoint returns empty array (not error)
- [ ] Grocery list generation completes in < 60s (testing timeout)

---

## 13.RAG — RAG Team Scope

> **Repo:** `rag-pipeline-hybrid-reterival`  
> **Owner:** RAG Pipeline Engineer  
> **The B2C team does NOT touch these files.**

### Deliverables

#### 1. `POST /recommend/products` Endpoint

Match ingredients to allergen-safe products with budget awareness:

**Request:**

```json
{
  "ingredient_ids": ["uuid-1", "uuid-2"],
  "customer_allergens": ["uuid-gluten", "uuid-dairy"]
}
```

**Response:**

```json
{
  "products": [
    {
      "ingredient_id": "uuid-1",
      "product_id": "prod-uuid",
      "product_name": "Bob's Red Mill GF Flour",
      "brand": "Bob's Red Mill",
      "price": 5.99,
      "currency": "USD",
      "weight_g": 680,
      "category": "Baking",
      "image_url": "...",
      "match_reason": "Allergen-safe (gluten-free), best price in category"
    }
  ]
}
```

#### 2. `POST /recommend/alternatives` Endpoint (shared with PRD-14)

See PRD-14 RAG section — same endpoint used for both grocery substitutions and scanner alternatives.

#### 3. Cypher for `[:CAN_SUBSTITUTE]` Traversal

```cypher
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

## 13.5 Route Registration

Add to `server/routes.ts`:

```typescript
// Already registered — just add the new sub-route:
// GET /api/v1/grocery-lists/:id/substitutions/:itemId
```

## 13.6 Environment Variables

```env
USE_GRAPH_GROCERY=false  # Set to 'true' to enable graph-enhanced grocery matching
```
