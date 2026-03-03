# PRD 15: Graph-Enhanced Meal Log Pattern Analysis

> **Tech Stack:** Next.js (frontend), Express + Drizzle ORM (backend), FastAPI (RAG API), Neo4j 5 (graph DB), PostgreSQL `gold` schema  
> **Auth:** Appwrite (B2C auth)  
> **Repos:** `nutrib2c-frontend` (frontend), `nutrition-backend-b2c` (backend), `rag-pipeline-hybrid-reterival` (RAG API)  
> **Family Context:** All features support household/family member switching. The `households` table is the root entity; each member is a `b2c_customers` row linked via `household_id`.  
> **Depends On:** PRD-09 (Foundation & Resilience)

---

## 15.1 Overview

Add graph-powered pattern analysis to the meal log feature: variety scoring (how diverse is the user's diet across cuisines and ingredients), repeat meal detection (eating the same thing too often), nutritional gap trends over time, and personalized suggestions to improve eating habits. The graph can traverse `[:LOGGED_MEAL]` → `[:OF_RECIPE]` → `[:USES_INGREDIENT]` → ingredient categories to compute diversity metrics that SQL can't efficiently do.

**Why this matters:** The current meal log in `mealLog.ts` (772 lines) tracks individual meals with calorie/macro totals, streaks, and basic history. But it can't answer "How diverse is my diet?" or "Am I eating too much of the same cuisine?" because answering those requires multi-hop graph traversals across meal→recipe→ingredient→cuisine paths.

**Current State:**

- Backend: `server/services/mealLog.ts` — `logMeal()`, `getHistory()`, `updateStreak()`, daily/weekly nutrition aggregates. All SQL-based.
- Frontend: Meal log page with history list, daily nutrition summary, streak display.

**SQL Fallback:** If `USE_GRAPH_MEAL_LOG=false` or RAG API is down → existing `getHistory()` with its SQL-based nutrition aggregates. Pattern analysis section simply doesn't appear or shows "Not enough data."

## 15.2 User Stories

| ID | Story | Priority |
|----|-------|----------|
| MLP-1 | As a user, I see a variety score (0–100) showing how diverse my meals have been | P0 |
| MLP-2 | As a user, I see which meals I've been repeating too often ("Chicken pasta 5 times in 7 days") | P0 |
| MLP-3 | As a user, I see a breakdown of cuisine diversity ("80% Italian, 15% Asian, 5% Other") | P1 |
| MLP-4 | As a user, I see nutritional trend lines (protein/carbs/fat) over 7/14/30 days | P1 |
| MLP-5 | As a user, I see suggestions to improve variety ("Try a Mediterranean recipe this week") | P1 |
| MLP-6 | As a user, the basic meal log works normally even if pattern analysis is unavailable | P0 |

## 15.3 Technical Architecture

### 15.3.1 Backend API

#### [NEW] `server/routes/mealLog.ts` — new pattern endpoint

```typescript
import { ragMealPatterns } from "./ragClient.js";
import { getHistory } from "./services/mealLog.js";

// GET /api/v1/meal-log/patterns?days=14
router.get("/patterns", requireAuth, async (req, res) => {
  const days = parseInt(req.query.days as string) || 14;
  const customerId = req.user.id;

  // Try graph-based pattern analysis
  const graphPatterns = await ragMealPatterns(customerId, days);

  if (graphPatterns) {
    res.json(graphPatterns);
    return;
  }

  // SQL fallback: basic stats from meal log history
  const history = await getHistory(customerId, days * 3, 0); // fetch enough for analysis
  const sqlPatterns = computeBasicPatterns(history, days);
  res.json(sqlPatterns);
});

// SQL fallback: basic pattern computation
function computeBasicPatterns(history: any[], days: number) {
  const recipeCounts = new Map<string, number>();
  let totalCalories = 0;
  let totalProtein = 0;
  let count = 0;

  for (const entry of history) {
    for (const item of entry.items) {
      if (item.recipeId) {
        recipeCounts.set(item.recipeId, (recipeCounts.get(item.recipeId) || 0) + 1);
      }
    }
    totalCalories += entry.totalCalories || 0;
    totalProtein += entry.totalProteinG || 0;
    count++;
  }

  const repeats = [...recipeCounts.entries()]
    .filter(([_, c]) => c >= 3)
    .map(([id, c]) => ({ recipeId: id, count: c }));

  return {
    varietyScore: null, // Can't compute without graph
    repeatedMeals: repeats,
    cuisineBreakdown: null, // Can't compute without graph
    avgDailyCalories: count ? Math.round(totalCalories / count) : null,
    avgDailyProtein: count ? Math.round(totalProtein / count) : null,
    suggestions: [],
    source: "sql_fallback",
  };
}
```

### 15.3.2 RAG API Endpoint

`POST /analytics/meal-patterns`:

```json
// Request
{
  "customer_id": "uuid",
  "days": 14
}

// Response
{
  "varietyScore": 72,
  "repeatedMeals": [
    { "recipeId": "uuid", "title": "Chicken Pasta", "count": 5, "lastEaten": "2026-03-10" }
  ],
  "cuisineBreakdown": [
    { "cuisine": "Italian", "percentage": 55 },
    { "cuisine": "Asian", "percentage": 25 },
    { "cuisine": "Mexican", "percentage": 12 },
    { "cuisine": "Other", "percentage": 8 }
  ],
  "nutritionTrends": {
    "daily": [
      { "date": "2026-03-01", "calories": 1850, "proteinG": 62, "carbsG": 220, "fatG": 55 }
    ]
  },
  "suggestions": [
    "Try a Mediterranean recipe — you haven't had one in 18 days",
    "Your protein intake dropped 15% this week vs last week"
  ],
  "source": "graph"
}
```

### 15.3.3 Graph Traversal for Patterns

```cypher
// Variety score: count unique cuisines and ingredients in last N days
MATCH (c:B2CCustomer {id: $customerId})-[:LOGGED_MEAL]->(ml:MealLog)
WHERE ml.log_date >= date() - duration({days: $days})
MATCH (ml)-[:CONTAINS_ITEM]->(mli:MealLogItem)-[:OF_RECIPE]->(r:Recipe)
OPTIONAL MATCH (r)-[:HAS_CUISINE]->(cuisine:Cuisine)
OPTIONAL MATCH (r)-[:USES_INGREDIENT]->(ing:Ingredient)
RETURN
  count(DISTINCT cuisine.id) AS uniqueCuisines,
  count(DISTINCT ing.id) AS uniqueIngredients,
  count(DISTINCT r.id) AS uniqueRecipes,
  count(mli) AS totalMeals
```

### 15.3.4 Database Tables Used (Already Exist)

| Table | Usage |
|-------|-------|
| `gold.meal_logs` + `gold.meal_log_items` | SQL fallback history data |
| `gold.meal_log_streaks` | Streak display (always SQL) |
| `gold.recipes` | Hydrate recipe names for repeat detection |

### 15.3.5 Frontend Changes

| File | Change |
|------|--------|
| **[NEW]** `components/meal-log/pattern-dashboard.tsx` | Dashboard card with variety score, cuisine chart, repeats |
| **[NEW]** `components/meal-log/variety-score.tsx` | Circular progress showing 0–100 variety score |
| **[NEW]** `components/meal-log/cuisine-chart.tsx` | Donut chart of cuisine breakdown |
| **[NEW]** `components/meal-log/repeated-meals.tsx` | List of over-repeated meals |
| **[NEW]** `components/meal-log/nutrition-trend.tsx` | Line chart of protein/carb/fat over time |
| Meal log page | Add pattern dashboard section |

## 15.4 Acceptance Criteria

- [ ] Pattern dashboard shows variety score (0–100) for logged meals
- [ ] Repeated meals highlighted when eaten ≥ 3 times in the analysis period
- [ ] Cuisine breakdown chart renders with percentages
- [ ] Nutrition trend lines display for 7/14/30 day windows
- [ ] At least 1 actionable suggestion appears when patterns are detected
- [ ] When `USE_GRAPH_MEAL_LOG=false`, basic SQL patterns still appear (repeat detection)
- [ ] When RAG is down, pattern section shows SQL fallback data or "Not enough data"
- [ ] Pattern analysis completes within 60s (testing timeout)

---

## 15.RAG — RAG Team Scope

> **Repo:** `rag-pipeline-hybrid-reterival`  
> **Owner:** RAG Pipeline Engineer  
> **The B2C team does NOT touch these files.**

### Deliverables

#### 1. `POST /analytics/meal-patterns` Endpoint

Analyze a user's meal log history for variety, repeats, and nutritional trends:

**Request:**

```json
{
  "customer_id": "uuid",
  "days": 14
}
```

**Response:**

```json
{
  "varietyScore": 72,
  "repeatedMeals": [
    { "recipeId": "uuid", "title": "Chicken Pasta", "count": 5, "lastEaten": "2026-03-10" }
  ],
  "cuisineBreakdown": [
    { "cuisine": "Italian", "percentage": 55 },
    { "cuisine": "Asian", "percentage": 25 }
  ],
  "nutritionTrends": {
    "daily": [
      { "date": "2026-03-01", "calories": 1850, "proteinG": 62, "carbsG": 220, "fatG": 55 }
    ]
  },
  "suggestions": [
    "Try a Mediterranean recipe — you haven't had one in 18 days",
    "Your protein intake dropped 15% this week vs last week"
  ],
  "source": "graph"
}
```

#### 2. Graph Traversal for Variety Score

Multi-hop Cypher: `B2CCustomer→[:LOGGED_MEAL]→MealLog→[:CONTAINS_ITEM]→MealLogItem→[:OF_RECIPE]→Recipe→[:HAS_CUISINE]→Cuisine` and `Recipe→[:USES_INGREDIENT]→Ingredient`

Variety score formula: `(uniqueCuisines / totalCuisines) * 0.4 + (uniqueIngredients / totalIngredients) * 0.3 + (uniqueRecipes / totalMeals) * 0.3`

#### 3. Suggestion Generation

Generate 1–3 actionable suggestions based on detected patterns:

- "Haven't tried {cuisine} in {N} days" (variety gap)
- "Protein intake dropped {X}% this week" (nutritional trend)
- "You've had {recipe} {N} times — try something new" (repeat detection)

## 15.5 Route Registration

Add to `server/routes.ts`:

```typescript
// Add pattern endpoint to existing meal log router:
// GET /api/v1/meal-log/patterns?days=14
```

## 15.6 Environment Variables

```env
USE_GRAPH_MEAL_LOG=false  # Set to 'true' to enable graph-enhanced meal log patterns
```
