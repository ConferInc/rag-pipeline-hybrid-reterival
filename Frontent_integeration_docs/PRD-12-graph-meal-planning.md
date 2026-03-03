# PRD 12: Graph-Enhanced Meal Planning

> **Tech Stack:** Next.js (frontend), Express + Drizzle ORM (backend), FastAPI (RAG API), Neo4j 5 (graph DB), PostgreSQL `gold` schema, LiteLLM proxy → OpenAI models  
> **Auth:** Appwrite (B2C auth)  
> **Repos:** `nutrib2c-frontend` (frontend), `nutrition-backend-b2c` (backend), `rag-pipeline-hybrid-reterival` (RAG API)  
> **Family Context:** All features support household/family member switching. The `households` table is the root entity; each member is a `b2c_customers` row linked via `household_id`.  
> **Depends On:** PRD-09 (Foundation & Resilience), PRD-10 (Graph Search — for `hydrateRecipesByIds`)

---

## 12.1 Overview

Upgrade meal plan generation to use graph-scored recipe candidates instead of the raw SQL recipe catalog. Currently, `generateMealPlan()` fetches up to 100 recipes from SQL and sends the entire catalog to the LLM. With graph scoring, the RAG pipeline pre-scores and ranks the top 50 candidates based on dietary compliance, nutritional gap analysis, variety (what the user ate recently), and collaborative filtering — then LLM gets a much better shortlist to work with.

**Why this matters:** Sending 100 unranked recipes to the LLM means it has to figure out which ones fit the user's needs. With graph-scored candidates, the LLM receives 50 pre-vetted recipes with scores like "fills protein gap" and "hasn't been eaten in 2 weeks." This produces higher-quality meal plans, fewer hallucinated recipe IDs, and measurably better nutritional coverage.

**Current State:**

- Backend: `server/services/mealPlan.ts` (776 lines) — fully functional: `generateMealPlan()`, `fetchRecipeCatalog()`, `buildRuleBasedFallbackPlan()`, `swapMeal()`, `activatePlan()`. LLM generation uses `mealPlanLLM.ts`.
- Frontend: Meal plan UI exists (`components/meal-plan/` — calendar, meal cards, swap modal, summary).

**SQL Fallback:** If `USE_GRAPH_MEAL_PLAN=false` or RAG API is down → existing `fetchRecipeCatalog()` provides unranked SQL results to the LLM. Plans are less personalized but still valid.

## 12.2 User Stories

| ID | Story | Priority |
|----|-------|----------|
| GMP-1 | As a user, my meal plan includes recipes that fill nutritional gaps in my recent eating history | P0 |
| GMP-2 | As a user, my meal plan avoids recipes I've eaten in the last 2 weeks | P0 |
| GMP-3 | As a user, I see a tooltip on each meal card explaining why it was selected | P1 |
| GMP-4 | As a user, when I swap a meal, the alternatives are ranked by graph scores | P1 |
| GMP-5 | As a user, meal plans generate correctly even if the graph database is down | P0 |
| GMP-6 | As a user, meal plans respect all family member allergens and dietary preferences | P0 |

## 12.3 Technical Architecture

### 12.3.1 Backend API

#### [MODIFY] `server/services/mealPlan.ts`

Replace the `fetchRecipeCatalog()` call inside `generateMealPlan()`:

```typescript
import { ragMealCandidates } from "./ragClient.js";

async function getRecipeCandidates(context: PlanGenerationContext): Promise<RecipeOption[]> {
  // Try graph-scored candidates first
  const graphCandidates = await ragMealCandidates({
    customer_id: context.customerId,
    members: context.members.map(m => ({
      id: m.id,
      allergen_ids: m.allergenIds,
      diet_ids: m.dietIds,
      health_profile: m.healthProfile,
    })),
    meal_history: context.recentRecipeIds,
    date_range: { start: context.startDate, end: context.endDate },
    meals_per_day: context.mealsPerDay,
    limit: 50,
  });

  if (graphCandidates) {
    // Graph returned pre-scored candidates with reasons
    return graphCandidates.candidates.map((c: any) => ({
      id: c.recipe_id,
      title: c.title,
      graphScore: c.score,
      graphReasons: c.reasons,
      // ... nutrition fields from PG hydration
    }));
  }

  // SQL fallback: existing fetchRecipeCatalog() — no graph scoring
  return fetchRecipeCatalog({
    cuisineIds: context.cuisineIds,
    maxCookTime: context.maxCookTime,
    excludeIds: context.recentRecipeIds,
    limit: 100,
  });
}
```

#### [MODIFY] `server/services/mealPlanLLM.ts`

Update `PLAN_SYSTEM_PROMPT` to utilize graph scores when available:

```typescript
// Add to prompt when graph candidates are available:
const graphContext = candidates.some(c => c.graphScore)
  ? `\nEach recipe has a graph_score (0-1) and reasons. Prefer higher-scored recipes.
     The scores already account for dietary compliance, nutritional gaps, and variety.
     Only deviate from scores if needed for meal type diversity.\n`
  : "";
```

#### [MODIFY] `swapMeal()` in `server/services/mealPlan.ts`

When a user swaps a meal, try graph-ranked alternatives first:

```typescript
async function getSwapAlternatives(
  currentRecipeId: string,
  mealType: string,
  context: PlanGenerationContext
): Promise<RecipeOption[]> {
  const graphAlts = await ragMealCandidates({
    customer_id: context.customerId,
    members: context.members,
    meal_type: mealType,
    exclude_ids: [currentRecipeId],
    limit: 5,
  });

  if (graphAlts) {
    return graphAlts.candidates;
  }

  // SQL fallback: existing fetchRecipeCatalog + buildRuleBasedSwapFallback
  const sqlAlts = await fetchRecipeCatalog({ ... });
  return buildRuleBasedSwapFallback(sqlAlts, currentRecipeId);
}
```

### 12.3.2 RAG API Endpoint

`POST /recommend/meal-candidates`:

```json
// Request
{
  "customer_id": "uuid",
  "members": [
    {
      "id": "member-uuid",
      "allergen_ids": ["uuid"],
      "diet_ids": ["uuid"],
      "health_profile": { "calorie_target": 2000, "protein_target_g": 60 }
    }
  ],
  "meal_history": ["recipe-uuid-1", "recipe-uuid-2"],
  "date_range": { "start": "2026-03-01", "end": "2026-03-07" },
  "meals_per_day": ["breakfast", "lunch", "dinner"],
  "limit": 50
}

// Response
{
  "candidates": [
    {
      "recipe_id": "uuid",
      "title": "Mediterranean Quinoa Bowl",
      "score": 0.91,
      "reasons": [
        "Fills protein gap (you average 42g vs 60g target)",
        "Haven't eaten Mediterranean in 12 days",
        "All ingredients allergen-safe for household"
      ]
    }
  ]
}
```

### 12.3.3 Database Tables Used (Already Exist)

| Table | Usage |
|-------|-------|
| `gold.meal_plans` + `gold.meal_plan_items` | Store generated plans |
| `gold.recipes` | Recipe catalog for SQL fallback |
| `gold.recipe_ratings` | Exclude poorly-rated recipes |
| `gold.b2c_customer_health_profiles` | Per-member nutrition targets |
| `gold.b2c_customer_allergens` | Allergen exclusions |
| `gold.meal_logs` + `gold.meal_log_items` | Recent eating history (graph variety scoring) |

### 12.3.4 Frontend Changes

| File | Change |
|------|--------|
| `components/meal-plan/meal-card.tsx` | Add `<Tooltip>` with graph reason on hover |
| `components/meal-plan/swap-modal.tsx` | Show graph-ranked alternatives with reason badges |
| `components/meal-plan/plan-summary.tsx` | Show "Graph-optimized" indicator when graph was used |

**Meal Card Tooltip:**

```tsx
<Tooltip>
  <TooltipTrigger>
    <Info className="w-3 h-3 text-muted-foreground" />
  </TooltipTrigger>
  <TooltipContent>
    <p className="text-sm font-medium">Why this recipe?</p>
    <ul className="text-xs mt-1 space-y-0.5">
      {reasons.map(r => <li key={r}>• {r}</li>)}
    </ul>
  </TooltipContent>
</Tooltip>
```

## 12.4 Acceptance Criteria

- [ ] Meal plan generation uses graph-scored candidates when `USE_GRAPH_MEAL_PLAN=true`
- [ ] Generated plan avoids recipes eaten in the last 14 days
- [ ] Graph reasons appear in meal card tooltips
- [ ] Swap alternatives are graph-ranked when available
- [ ] Plan generation still works when RAG is down (SQL fallback)
- [ ] LLM prompt includes graph scores when available
- [ ] Plan respects all member allergens (zero violations)
- [ ] Plan generation completes within 60s (testing timeout)

---

## 12.RAG — RAG Team Scope

> **Repo:** `rag-pipeline-hybrid-reterival`  
> **Owner:** RAG Pipeline Engineer  
> **The B2C team does NOT touch these files.**

### Deliverables

#### 1. `POST /recommend/meal-candidates` Endpoint

Return top-N pre-scored recipe candidates for meal plan generation:

**Request:**

```json
{
  "customer_id": "uuid",
  "members": [
    {
      "id": "member-uuid",
      "allergen_ids": ["uuid"],
      "diet_ids": ["uuid"],
      "health_profile": { "calorie_target": 2000, "protein_target_g": 60 }
    }
  ],
  "meal_history": ["recipe-uuid-1", "recipe-uuid-2"],
  "date_range": { "start": "2026-03-01", "end": "2026-03-07" },
  "meals_per_day": ["breakfast", "lunch", "dinner"],
  "limit": 50
}
```

**Response:**

```json
{
  "candidates": [
    {
      "recipe_id": "uuid",
      "title": "Mediterranean Quinoa Bowl",
      "score": 0.91,
      "reasons": [
        "Fills protein gap (42g vs 60g target)",
        "Haven't eaten Mediterranean in 12 days",
        "All ingredients allergen-safe for household"
      ]
    }
  ]
}
```

#### 2. Graph Scoring Logic

For each candidate recipe, compute a combined score:

- **Allergen safety (hard filter):** Exclude ANY recipe with allergens matching ANY household member
- **Diet compliance:** Score based on `[:FOLLOWS_DIET]` edge overlap
- **Variety:** Penalize recipes with same cuisine as `meal_history` items (Cypher: `MATCH (r)-[:HAS_CUISINE]->(c) WHERE c.id IN $recentCuisines`)
- **Nutritional gap:** Boost recipes that fill nutrient shortfalls based on `HealthProfile` targets vs recent `MealLog` actuals
- **Freshness:** Penalize recipes already in `meal_history` (already eaten recently)

#### 3. Cypher for Variety Scoring

```cypher
MATCH (c:B2CCustomer {id: $customerId})-[:LOGGED_MEAL]->(ml:MealLog)
WHERE ml.log_date >= date() - duration({days: 14})
MATCH (ml)-[:CONTAINS_ITEM]->(mli:MealLogItem)-[:OF_RECIPE]->(r:Recipe)
OPTIONAL MATCH (r)-[:HAS_CUISINE]->(cuisine:Cuisine)
RETURN collect(DISTINCT r.id) AS recentRecipeIds,
       collect(DISTINCT cuisine.id) AS recentCuisineIds
```

## 12.5 Route Registration

No new routes — modifies existing meal plan endpoints.

## 12.6 Environment Variables

```env
USE_GRAPH_MEAL_PLAN=false  # Set to 'true' to enable graph-scored meal planning
```
