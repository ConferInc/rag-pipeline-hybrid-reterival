# PRD 11: Graph-Enhanced Personalized Feed

> **Tech Stack:** Next.js (frontend), Express + Drizzle ORM (backend), FastAPI (RAG API), Neo4j 5 (graph DB), PostgreSQL `gold` schema  
> **Auth:** Appwrite (B2C auth)  
> **Repos:** `nutrib2c-frontend` (frontend), `nutrition-backend-b2c` (backend), `rag-pipeline-hybrid-reterival` (RAG API)  
> **Family Context:** All features support household/family member switching. The `households` table is the root entity; each member is a `b2c_customers` row linked via `household_id`.  
> **Depends On:** PRD-09 (Foundation & Resilience)

---

## 11.1 Overview

Upgrade the dashboard recipe feed from SQL-based popularity sorting to graph-powered personalized recommendations with explainable reasons. The graph considers collaborative filtering (users with similar profiles liked these recipes), dietary compliance, nutritional gap filling, and recipe variety — then provides human-readable reasons like "Fills your protein gap" or "Popular with users like you."

**Why this matters:** The current `getPersonalizedFeed()` in `feed.ts` sorts by `saved_30d DESC` (popularity) and filters out viewed/allergen recipes via SQL. It doesn't know what *similar* users liked, doesn't detect nutritional gaps, and can't explain *why* a recipe is recommended. Graph-based recommendations add collaborative filtering and explainability.

**Current State:**

- Backend: `server/services/feed.ts` → `getPersonalizedFeed()` (238 lines) + `getFeedRecommendations()` → returns trending/forYou/recent sections. Sorting is by 30-day save count + recency.
- Frontend: Dashboard shows recipe feed cards. No reason badges or explanations.

**SQL Fallback:** If `USE_GRAPH_FEED=false` or RAG API is down → existing `getPersonalizedFeed()`. Feed looks identical, just without graph-based personalization or reason explanations.

## 11.2 User Stories

| ID | Story | Priority |
|----|-------|----------|
| PF-1 | As a user, my feed is personalized based on my dietary profile and interaction history | P0 |
| PF-2 | As a user, I see small reason badges on feed cards explaining why a recipe is recommended | P0 |
| PF-3 | As a user, I see recipes that fill nutritional gaps (e.g., "Fills your protein gap") | P1 |
| PF-4 | As a user, I see recipes that similar users have enjoyed | P1 |
| PF-5 | As a user, the feed still works normally if the recommendation engine is down | P0 |
| PF-6 | As a user, I don't see recipes I've already viewed in the last 48 hours | P0 |

## 11.3 Technical Architecture

### 11.3.1 Backend API

#### [MODIFY] `server/services/feed.ts`

```typescript
import { ragFeed } from "./ragClient.js";

export async function getPersonalizedFeedWithRAG(
  b2cCustomerId: string,
  limit: number = 200,
  offset: number = 0
): Promise<FeedResult[]> {
  // Try graph-powered personalization first
  const prefs = await getUserPrefs(b2cCustomerId);
  const graphFeed = await ragFeed(b2cCustomerId, prefs);

  if (graphFeed) {
    // Graph returned scored + explained results — hydrate from PG
    const ids = graphFeed.results.map((r: any) => r.id);
    const hydrated = await hydrateRecipesByIds(ids);
    const nutritionMap = await getRecipeNutritionMap(ids);
    const allergenMap = await getRecipeAllergenMap(ids);

    return hydrated.map((recipe, i) => ({
      recipe: mapFeedRecipe(recipe, nutritionMap, allergenMap),
      score: graphFeed.results[i]?.score ?? 0,
      reasons: graphFeed.results[i]?.reasons ?? [],
    }));
  }

  // SQL fallback — existing logic (popularity + recency)
  return getPersonalizedFeed(b2cCustomerId, limit, offset);
}

export async function getFeedRecommendationsWithRAG(b2cCustomerId: string) {
  const forYou = await getPersonalizedFeedWithRAG(b2cCustomerId, 20);

  // Trending and recent always use SQL (not personalized)
  const trending = await getTrendingRecipes();
  const recent = await getRecentRecipes();

  return { trending, forYou, recent };
}
```

#### [MODIFY] `server/routes/feed.ts` or `server/routes/recipes.ts`

```typescript
router.get("/feed", requireAuth, async (req, res) => {
  const recommendations = await getFeedRecommendationsWithRAG(req.user.id);
  res.json(recommendations);
});
```

### 11.3.2 RAG API Endpoint

`POST /recommend/feed`:

```json
// Request
{
  "customer_id": "uuid",
  "preferences": {
    "dietIds": ["uuid", "uuid"],
    "allergenIds": ["uuid"],
    "conditionIds": [],
    "dislikes": ["olives"]
  }
}

// Response
{
  "results": [
    {
      "id": "recipe-uuid",
      "score": 0.87,
      "reasons": [
        "Matches your Mediterranean diet",
        "Fills your protein gap (you average 42g vs 60g target)",
        "Popular with users who share your preferences"
      ],
      "source": "collaborative_filtering"
    }
  ]
}
```

### 11.3.3 Reason Types

The graph can generate these reason categories:

| Reason Type | Example | Data Source |
|------------|---------|-------------|
| Diet match | "Matches your Vegan diet" | `[:FOLLOWS_DIET]` edge |
| Allergen safe | "Allergen-safe for your household" | `[:ALLERGIC_TO]` edge check |
| Nutritional gap | "Fills your protein gap (42g/60g)" | `HealthProfile` node |
| Collaborative | "Popular with users like you" | GraphSAGE embeddings |
| Variety | "Haven't tried Italian in 2 weeks" | `[:LOGGED_MEAL]` history |
| Trending | "Saved 47 times this week" | `[:SAVED]` edge count |

### 11.3.4 Database Tables Used (Already Exist)

| Table | Usage |
|-------|-------|
| `gold.recipes` | Hydrate graph results |
| `gold.b2c_customer_dietary_preferences` | User diet context for graph |
| `gold.b2c_customer_allergens` | User allergen context for graph |
| `gold.b2c_customer_health_profiles` | Nutrition targets for gap detection |
| `gold.customer_product_interactions` | SQL fallback trending calculation |

### 11.3.5 Frontend Changes

| File | Change |
|------|--------|
| Dashboard feed component | Render `reasons[]` as badges on feed cards |
| **[NEW]** `components/feed/recommendation-reason.tsx` | Small pill/badge for reason text |
| Feed card component | Add tooltip with full explanation on hover |

**Recommendation Reason Component:**

```tsx
// components/feed/recommendation-reason.tsx
import { Badge } from "@/components/ui/badge";
import { Sparkles, Shield, TrendingUp, Utensils } from "lucide-react";

const ICONS: Record<string, any> = {
  diet: Utensils,
  allergen: Shield,
  nutritional: TrendingUp,
  default: Sparkles,
};

export function RecommendationReason({ reason }: { reason: string }) {
  const type = reason.includes("diet") ? "diet"
    : reason.includes("Allergen") ? "allergen"
    : reason.includes("protein") || reason.includes("gap") ? "nutritional"
    : "default";
  const Icon = ICONS[type];

  return (
    <Badge variant="secondary" className="text-xs gap-1">
      <Icon className="w-3 h-3" />
      {reason}
    </Badge>
  );
}
```

## 11.4 Acceptance Criteria

- [ ] Feed shows personalized results with reason badges when `USE_GRAPH_FEED=true`
- [ ] Reasons include at least diet match and allergen safety when applicable
- [ ] Feed shows at least 20 recipes per page
- [ ] When `USE_GRAPH_FEED=false`, feed falls back to SQL popularity sorting
- [ ] Already-viewed recipes (48h) are excluded
- [ ] Trending and recent sections always work (SQL-only, no graph dependency)
- [ ] Feed loads within 60s (testing timeout)

---

## 11.RAG — RAG Team Scope

> **Repo:** `rag-pipeline-hybrid-reterival`  
> **Owner:** RAG Pipeline Engineer  
> **The B2C team does NOT touch these files.**

### Deliverables

#### 1. `POST /recommend/feed` Endpoint

Score and rank recipes for a specific user based on graph signals:

**Request:**

```json
{
  "customer_id": "uuid",
  "preferences": {
    "dietIds": ["uuid"],
    "allergenIds": ["uuid"],
    "conditionIds": [],
    "dislikes": ["olives"]
  }
}
```

**Response:**

```json
{
  "results": [
    {
      "id": "recipe-uuid",
      "score": 0.87,
      "reasons": [
        "Matches your Mediterranean diet",
        "Fills your protein gap (42g/60g target)",
        "Popular with users who share your preferences"
      ],
      "source": "collaborative_filtering"
    }
  ]
}
```

#### 2. Scoring Logic

Combine multiple graph signals into a final score:

- **Structural score:** Diet compliance, allergen safety, cuisine match
- **Collaborative score:** GraphSAGE embedding similarity (see PRD-17)
- **Variety score:** Penalize recently-eaten cuisines/recipes
- **Nutritional gap score:** Boost recipes that fill nutrient deficiencies

#### 3. Reason Generation

For each recommended recipe, generate 1–3 human-readable reasons from the scoring signals:

| Signal | Reason Template |
|--------|----------------|
| Diet match | "Matches your {diet} diet" |
| Allergen safe | "Allergen-safe for your household" |
| Protein gap | "Fills your protein gap ({current}g/{target}g)" |
| Collaborative | "Popular with users who share your preferences" |
| Variety | "Haven't tried {cuisine} in {N} days" |

## 11.5 Route Registration

No new routes — modifies existing feed endpoint.

## 11.6 Environment Variables

```env
USE_GRAPH_FEED=false  # Set to 'true' to enable graph-enhanced feed
```
