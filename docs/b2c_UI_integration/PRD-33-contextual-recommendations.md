# PRD 33: Contextual Recommendations

> **Tech Stack:** Next.js (frontend), Express + Drizzle ORM (backend), FastAPI (RAG API), Neo4j 5 (graph DB), PostgreSQL `gold` schema, LiteLLM proxy → OpenAI models  
> **Auth:** Appwrite (B2C auth)  
> **Repos:** `nutrib2c-frontend` (frontend), `nutrition-backend-b2c` (backend), `rag-pipeline-hybrid-reterival` (RAG API)  
> **Family Context:** All features support household/family member switching. The `households` table is the root entity; each member is a `b2c_customers` row linked via `household_id`.  
> **Depends On:** PRD-09 (Foundation & Resilience), PRD-11 (Graph Feed), PRD-19 (Onboarding & Auth)

---

## 33.1 Overview

Enhance the feed, search, and recommendation engine to deliver **contextually relevant** food suggestions based on the user's location, timezone, season, cuisine preferences, and nutritional targets. Currently, recommendations are primarily driven by allergens, diets, and health conditions — but lack awareness of cultural context, time-of-day, seasonal relevance, and calorie/macro goals.

**Vijay Sir's Directive:**
> _"It needs to be contextual... somebody's using the app in India, they're going to eat dosa idli for breakfast. We cannot recommend them pancake."_ — (6:23–6:42)

> _"This is good about LLM... previously somebody has to create rules, now LLM just understands context from history."_ — (9:59–10:35)

**Current State:**

- **Onboarding** (`onboarding-context.tsx`) — captures dob, sex, height, weight, activity, goal, allergens, health conditions, dietary preferences. **NO location or cuisine**
- **Settings** (`user.ts`) — captures `preferredCuisines` via junction table. **NOT passed to RAG feed or search**
- **Schema** — `households` has `timezone` and `locationCountry` (both often unpopulated). Missing `zipCode`, `state`
- **RAG Pipeline** (`profile_enrichment.py`) — merges diets, allergens, health conditions only. **Zero cuisine/region/season/time/calorie awareness**
- **Feed Service** (`feed.ts`) — `ragFeed()` called without any time/region/cuisine/calorie context
- **Target Calories/Macros** — stored in `b2cCustomerHealthProfiles` and used for meal plans BUT **not for feed/search**

**Target State:**

- User's location captured during onboarding (hybrid: auto-detect timezone + ask for zip/country)
- Timezone, season, time-of-day, cuisine preferences, and calorie/macro targets passed as context to all RAG calls
- RAG pipeline enriches queries with contextual signals for culturally relevant, time-appropriate, nutritionally aligned recommendations

## 33.2 User Stories

| ID | Story | Priority |
|----|-------|----------|
| CR-1 | As a user in India, I see Indian cuisine recommendations (dosa, idli) for breakfast, not pancakes | P0 |
| CR-2 | As a user, I see seasonally appropriate food (warm soups in winter, salads in summer) | P0 |
| CR-3 | As a user, I see breakfast recipes in the morning feed, not dinner recipes | P0 |
| CR-4 | As a user, my feed respects my calorie target — no 900cal recipes when my target is 1800cal/day | P0 |
| CR-5 | As a user, I set my location (country + zip) during onboarding for regional recommendations | P0 |
| CR-6 | As a user, my preferred cuisines (set in Settings) influence my feed and search results | P1 |
| CR-7 | As a user, recommendations improve over time as the system learns from my meal history | P1 |
| CR-8 | As a user, family member context (allergens, cuisine prefs) is applied when viewing feed for a specific member | P1 |

## 33.3 Technical Architecture

### 33.3.1 Context Model

Every RAG call (feed, search, chat) will include a `context` object alongside the existing profile data:

```typescript
interface RecommendationContext {
  // Location-based (from households table)
  timezone: string;              // e.g., "America/New_York"
  country: string | null;        // e.g., "US", "India"
  state: string | null;          // e.g., "California", "Maharashtra"
  zipCode: string | null;        // e.g., "94105"

  // Time-derived (auto-calculated)
  mealTimeSlot: "morning" | "afternoon" | "evening" | "late_night";
  season: "spring" | "summer" | "fall" | "winter";
  dayOfWeek: string;             // "monday" .. "sunday"
  isWeekend: boolean;

  // Preference-based (from DB)
  cuisinePreferences: string[];  // e.g., ["Indian", "Mediterranean"]
  
  // Nutritional targets (from b2cCustomerHealthProfiles)
  targetCalories: number | null;
  targetProteinG: number | null;
  targetCarbsG: number | null;
  targetFatG: number | null;
  targetFiberG: number | null;
  targetSugarG: number | null;
  targetSodiumMg: number | null;

  // Recent history (auto-derived from meal_logs)
  recentMealIds: string[];       // Last 3 days of logged recipe IDs (to avoid repetition)
}
```

### 33.3.2 Context Derivation Functions

#### [NEW] `server/services/contextBuilder.ts`

```typescript
import { executeRaw } from "../config/database.js";

/**
 * Derive meal time slot from timezone + current server time
 */
export function deriveMealTimeSlot(timezone: string): RecommendationContext["mealTimeSlot"] {
  const now = new Date();
  const localHour = getLocalHour(now, timezone);
  
  if (localHour >= 5 && localHour < 12)  return "morning";
  if (localHour >= 12 && localHour < 17) return "afternoon";
  if (localHour >= 17 && localHour < 22) return "evening";
  return "late_night";
}

/**
 * Derive season from timezone (hemisphere detection) + current date
 */
export function deriveSeason(timezone: string): RecommendationContext["season"] {
  const month = new Date().getMonth() + 1; // 1-12
  const isNorthern = isNorthernHemisphere(timezone);
  
  if (month >= 3 && month <= 5)  return isNorthern ? "spring" : "fall";
  if (month >= 6 && month <= 8)  return isNorthern ? "summer" : "winter";
  if (month >= 9 && month <= 11) return isNorthern ? "fall" : "spring";
  return isNorthern ? "winter" : "summer";
}

/**
 * Get user's recent meal recipe IDs (last 3 days) for dedup
 */
export async function getRecentMealIds(customerId: string): Promise<string[]> {
  const rows = await executeRaw(
    `SELECT DISTINCT mli.recipe_id 
     FROM gold.meal_log_items mli
     JOIN gold.meal_logs ml ON ml.id = mli.meal_log_id
     WHERE ml.b2c_customer_id = $1 
       AND ml.log_date >= CURRENT_DATE - INTERVAL '3 days'
       AND mli.recipe_id IS NOT NULL`,
    [customerId]
  );
  return rows.map((r: any) => r.recipe_id);
}

/**
 * Get user's cuisine preferences from junction table
 */
export async function getCuisinePreferences(customerId: string): Promise<string[]> {
  const rows = await executeRaw(
    `SELECT c.name FROM gold.b2c_customer_cuisine_preferences cp
     JOIN gold.cuisines c ON c.id = cp.cuisine_id
     WHERE cp.b2c_customer_id = $1`,
    [customerId]
  );
  return rows.map((r: any) => r.name);
}

/**
 * Build complete context for a customer
 */
export async function buildRecommendationContext(
  customerId: string,
  household: { timezone?: string; locationCountry?: string; locationState?: string; locationZipCode?: string },
  healthProfile: { targetCalories?: number; targetProteinG?: number; targetCarbsG?: number; targetFatG?: number; targetFiberG?: number; targetSugarG?: number; targetSodiumMg?: number } | null
): Promise<RecommendationContext> {
  const tz = household.timezone ?? "UTC";
  const [cuisinePrefs, recentMeals] = await Promise.all([
    getCuisinePreferences(customerId),
    getRecentMealIds(customerId),
  ]);

  const now = new Date();

  return {
    timezone: tz,
    country: household.locationCountry ?? null,
    state: household.locationState ?? null,
    zipCode: household.locationZipCode ?? null,
    mealTimeSlot: deriveMealTimeSlot(tz),
    season: deriveSeason(tz),
    dayOfWeek: now.toLocaleDateString("en-US", { weekday: "long", timeZone: tz }).toLowerCase(),
    isWeekend: [0, 6].includes(now.getDay()),
    cuisinePreferences: cuisinePrefs,
    targetCalories: healthProfile?.targetCalories ?? null,
    targetProteinG: healthProfile?.targetProteinG ?? null,
    targetCarbsG: healthProfile?.targetCarbsG ?? null,
    targetFatG: healthProfile?.targetFatG ?? null,
    targetFiberG: healthProfile?.targetFiberG ?? null,
    targetSugarG: healthProfile?.targetSugarG ?? null,
    targetSodiumMg: healthProfile?.targetSodiumMg ?? null,
    recentMealIds: recentMeals,
  };
}
```

### 33.3.3 Schema Changes

#### [MODIFY] `shared/goldSchema.ts` — households table

```diff
  export const households = gold.table("households", {
    ...
    locationCountry: varchar("location_country", { length: 100 }),
+   locationState: varchar("location_state", { length: 100 }),
+   locationZipCode: varchar("location_zip_code", { length: 20 }),
    ...
  });
```

**Migration SQL:**

```sql
ALTER TABLE gold.households ADD COLUMN location_state VARCHAR(100);
ALTER TABLE gold.households ADD COLUMN location_zip_code VARCHAR(20);
```

> No new tables needed. Cuisine preferences already live in `gold.b2c_customer_cuisine_preferences`. Calorie/macro targets already live in `gold.b2c_customer_health_profiles`.

### 33.3.4 Backend Changes

#### [MODIFY] `server/services/ragClient.ts` — Add context to ragFeed

```diff
  export async function ragFeed(
    customerId: string,
    preferences: string,
    memberId: string | null,
    memberProfile: string | null,
    householdType: string | null,
    totalMembers: number | null,
    householdId: string,
    scope: string | null,
    mealType?: string,
+   context?: RecommendationContext
  ): Promise<RagFeedResult | null> {
    const body: Record<string, any> = {
      customer_id: customerId,
      preferences,
      member_id: memberId,
      member_profile: memberProfile,
      household_type: householdType,
      total_members: totalMembers,
      household_id: householdId,
      scope,
      meal_type: mealType,
+     context: context ?? undefined,
    };
    return callRag("/recommend/feed", body, "ragFeed") as any;
  }
```

#### [MODIFY] `server/services/ragClient.ts` — Add context to ragSearch

```diff
  export async function ragSearch(
    query: string,
    customerId: string,
    memberId: string | null,
    memberProfile: string | null,
+   context?: RecommendationContext
  ): Promise<RagSearchResult | null> {
    const body: Record<string, any> = {
      query,
      customer_id: customerId,
      member_id: memberId,
      member_profile: memberProfile,
+     context: context ?? undefined,
    };
    return callRag("/search", body, "ragSearch") as any;
  }
```

#### [MODIFY] `server/services/feed.ts` — Build context in getPersonalizedFeedWithRAG

```diff
+ import { buildRecommendationContext } from "./contextBuilder.js";

  async function getPersonalizedFeedWithRAG(...) {
+   // Build contextual recommendation context
+   const healthProfile = await getHealthProfile(b2cCustomerId);
+   const context = await buildRecommendationContext(
+     b2cCustomerId,
+     household,
+     healthProfile
+   );

    const graphFeed = await ragFeed(
      b2cCustomerId, prefs, memberId, memberProfile,
      household.householdType, household.totalMembers,
-     household.id, toRagScope(household.householdType)
+     household.id, toRagScope(household.householdType), undefined, context
    );
  }
```

### 33.3.5 Frontend Changes

#### [MODIFY] Onboarding — Add Location Step

Add a hybrid location capture step between "Personal Details" and "Lifestyle" (step 2.5):

| Element | Implementation |
|---------|---------------|
| Auto-detect timezone | `Intl.DateTimeFormat().resolvedOptions().timeZone` — saved automatically |
| Country dropdown | List of countries, defaulted from timezone prefix |
| State/Province (conditional) | Shown for US, Canada, India, UK — dropdown of states |
| ZIP/Postal Code (optional) | Free-text input, validated per country format |

#### [MODIFY] `app/onboarding/onboarding-context.tsx`

Add location fields to `OnboardingData`:

```diff
  export interface OnboardingData {
    dob: string;
    sex: Sex;
    // ... existing fields ...
+   country: string;
+   state: string;
+   zipCode: string;
  }
```

Add new step to the STEPS array:

```diff
  export const STEPS = [
    { path: "/onboarding/personal-info", label: "Personal Details", step: 2 },
+   { path: "/onboarding/location", label: "Location", step: 3 },
-   { path: "/onboarding/lifestyle", label: "Activity Level", step: 3 },
+   { path: "/onboarding/lifestyle", label: "Activity Level", step: 4 },
-   { path: "/onboarding/goals", label: "Goals", step: 4 },
+   { path: "/onboarding/goals", label: "Goals", step: 5 },
-   { path: "/onboarding/health", label: "Profile Setup", step: 5 },
+   { path: "/onboarding/health", label: "Profile Setup", step: 6 },
-   { path: "/onboarding/review", label: "Review", step: 6 },
+   { path: "/onboarding/review", label: "Review", step: 7 },
  ] as const;

- export const TOTAL_STEPS = 5;
+ export const TOTAL_STEPS = 6;
```

#### [NEW] `app/onboarding/location/page.tsx`

New onboarding step page for location capture. Layout:

```
┌────────────────────────────────────┐
│  📍 Where are you located?         │
│                                    │
│  Country:  [Auto-detected ▼]       │
│  State:    [Select state ▼]        │
│  ZIP Code: [___________] (optional)│
│                                    │
│  ℹ️ Used for regional food         │
│    recommendations & local pricing │
│                                    │
│            [Continue →]            │
└────────────────────────────────────┘
```

#### [MODIFY] Settings Page — Location Section

Add location fields to the Settings page (`app/(main)/profile/settings/page.tsx`):

| Field | Component | Source |
|-------|-----------|--------|
| Country | `<Select>` dropdown | `households.locationCountry` |
| State | `<Select>` dropdown (conditional) | `households.locationState` |
| ZIP Code | `<Input>` text | `households.locationZipCode` |

#### Backend Route — Accept Location in Settings

Add location fields to the `settingsSchema` in `user.ts`:

```diff
  const settingsSchema = z.object({
    // General
    units: z.string().optional(),
    preferredCuisines: z.array(z.string()).optional(),
+   locationCountry: z.string().optional(),
+   locationState: z.string().optional(),
+   locationZipCode: z.string().optional(),
    // ... rest unchanged
  });
```

In the PATCH handler, update household with location:

```diff
+ if (body.locationCountry !== undefined || body.locationState !== undefined || body.locationZipCode !== undefined) {
+   const household = await getOrCreateHousehold(id);
+   await db.update(households).set({
+     locationCountry: body.locationCountry,
+     locationState: body.locationState,
+     locationZipCode: body.locationZipCode,
+     updatedAt: new Date(),
+   }).where(eq(households.id, household.id));
+ }
```

## 33.RAG — RAG Team Scope

> **Repo:** `rag-pipeline-hybrid-reterival`  
> **Owner:** RAG Pipeline Engineer  
> **The B2C team does NOT touch these files.** This is the second-largest RAG deliverable after the chatbot.

### Deliverables

#### 1. Accept `context` in All Recommendation Endpoints

Update all existing endpoint handlers to accept and process the new `context` field:

| Endpoint | File | Change |
|----------|------|--------|
| `POST /recommend/feed` | `app.py` / router | Parse `context` from request body |
| `POST /search` | `app.py` / router | Parse `context` from request body |
| `POST /chat/process` | `app.py` / router | Parse `context` from request body |

**Context schema received from B2C backend:**

```json
{
  "context": {
    "timezone": "America/New_York",
    "country": "US",
    "state": "California",
    "zipCode": "94105",
    "mealTimeSlot": "morning",
    "season": "spring",
    "dayOfWeek": "monday",
    "isWeekend": false,
    "cuisinePreferences": ["Indian", "Mediterranean"],
    "targetCalories": 1800,
    "targetProteinG": 120,
    "targetCarbsG": 200,
    "targetFatG": 60,
    "targetFiberG": 30,
    "targetSugarG": 40,
    "targetSodiumMg": 2000,
    "recentMealIds": ["uuid-1", "uuid-2"]
  }
}
```

#### 2. Extend `profile_enrichment.py` — Consume Context

**File:** `rag_pipeline/orchestrator/profile_enrichment.py`

```diff
  def merge_profile_into_entities(entities, profile):
    result = dict(entities)
    # ... existing diet/allergen/condition merging ...

+   # ── Contextual enrichment ──
+   context = profile.get("context") or {}
+   
+   # Cuisine preferences → steer recipe selection
+   if context.get("cuisinePreferences"):
+     result["cuisine_preference"] = context["cuisinePreferences"]
+   
+   # Region → infer cultural food expectations
+   if context.get("country"):
+     result["region"] = context["country"]
+   if context.get("state"):
+     result["sub_region"] = context["state"]
+   
+   # Time → meal-type filtering
+   if context.get("mealTimeSlot"):
+     result["meal_time"] = context["mealTimeSlot"]
+   
+   # Season → seasonal produce and comfort food
+   if context.get("season"):
+     result["season"] = context["season"]
+   
+   # Nutritional targets → calorie-aware filtering
+   if context.get("targetCalories"):
+     result["calorie_target"] = context["targetCalories"]
+   if context.get("targetProteinG"):
+     result["protein_target_g"] = context["targetProteinG"]
+   
+   # Recent meals → avoid repetition
+   if context.get("recentMealIds"):
+     result["exclude_recipe_ids"] = context["recentMealIds"]

    return result
```

#### 3. Modify Orchestrator — Context-Aware Retrieval

**File:** `rag_pipeline/orchestrator/orchestrator.py`

Inject contextual signals into the query augmentation phase:

```python
def augment_query_with_context(query: str, entities: dict) -> str:
    """Add contextual signals to the search query for better retrieval"""
    augmented = query
    
    # Time-of-day → append meal type
    meal_time = entities.get("meal_time")
    if meal_time == "morning":
        augmented += " breakfast morning"
    elif meal_time == "afternoon":
        augmented += " lunch midday"
    elif meal_time == "evening":
        augmented += " dinner supper"
    
    # Season → seasonal food terms
    season = entities.get("season")
    if season == "summer":
        augmented += " fresh light cool refreshing salad"
    elif season == "winter":
        augmented += " warm hearty comfort soup stew"
    
    # Cuisine preference → cultural food terms
    cuisines = entities.get("cuisine_preference", [])
    if cuisines:
        augmented += " " + " ".join(cuisines[:3])
    
    return augmented
```

#### 4. Context-Aware Scoring / Re-ranking

After retrieval, apply contextual re-ranking:

```python
def contextual_rerank(results: list, entities: dict) -> list:
    """Boost/penalize results based on context"""
    calorie_target = entities.get("calorie_target")
    protein_target = entities.get("protein_target_g")
    exclude_ids = set(entities.get("exclude_recipe_ids", []))
    
    scored = []
    for r in results:
        score = r.get("score", 0.5)
        
        # Penalize recently eaten recipes
        if r.get("recipe_id") in exclude_ids:
            score *= 0.3
        
        # Penalize recipes way above calorie target per meal
        # (assuming 3 meals/day, each meal ~1/3 of daily target)
        if calorie_target and r.get("calories"):
            per_meal = calorie_target / 3
            if r["calories"] > per_meal * 1.5:
                score *= 0.7  # Penalize recipes >50% over per-meal target
        
        # Boost recipes that match cuisine preference
        recipe_cuisine = (r.get("cuisine") or "").lower()
        cuisines = [c.lower() for c in entities.get("cuisine_preference", [])]
        if recipe_cuisine in cuisines:
            score *= 1.3
        
        scored.append({**r, "score": score})
    
    return sorted(scored, key=lambda x: x["score"], reverse=True)
```

#### 5. LLM Prompt Context Injection

When generating natural-language responses (feed descriptions, chatbot answers), include context in the system prompt:

```python
def build_system_prompt(entities: dict) -> str:
    base = "You are a nutrition assistant..."
    
    context_parts = []
    if entities.get("meal_time"):
        context_parts.append(f"The user is looking for {entities['meal_time']} meal suggestions.")
    if entities.get("season"):
        context_parts.append(f"It's currently {entities['season']}.")
    if entities.get("region"):
        context_parts.append(f"The user is located in {entities['region']}.")
    if entities.get("cuisine_preference"):
        prefs = ", ".join(entities["cuisine_preference"])
        context_parts.append(f"They prefer: {prefs} cuisine.")
    if entities.get("calorie_target"):
        context_parts.append(f"Daily calorie target: {entities['calorie_target']} kcal.")
    
    if context_parts:
        base += "\n\nUser context:\n" + "\n".join(f"- {p}" for p in context_parts)
    
    return base
```

## 33.4 Acceptance Criteria

- [ ] New onboarding step captures country, state (optional), zip code (optional)
- [ ] Timezone auto-detected from browser and saved to `households.timezone`
- [ ] Morning feed shows breakfast-appropriate recipes
- [ ] Evening feed shows dinner-appropriate recipes
- [ ] Summer feed favors salads, fresh foods; winter favors soups, stews
- [ ] User with "Indian" cuisine preference sees Indian recipes prioritized
- [ ] User with `targetCalories=1800` does not see 900+ cal recipes in top results
- [ ] Recipes eaten in last 3 days are deprioritized
- [ ] Location fields appear in Settings and are editable
- [ ] Existing onboarding flow is not broken (dob, sex, height, weight, allergens still saved)
- [ ] All context fields are passed to RAG `/recommend/feed` and `/search`
- [ ] RAG returns contextually relevant results when context is provided
- [ ] Feed degrades gracefully when context fields are null/missing

## 33.5 Environment Variables

```env
# No new env vars needed — context is data-driven, not config-driven
# Existing RAG feature flags control whether RAG is used
USE_GRAPH_FEED=true
USE_GRAPH_SEARCH=true
```

## 33.6 Files Summary

| File | Action | Lines (est.) |
|------|--------|-------------|
| `shared/goldSchema.ts` | MODIFY | +5 (2 new columns on households) |
| **`server/services/contextBuilder.ts`** | **NEW** | ~120 (context derivation + building) |
| `server/services/ragClient.ts` | MODIFY | +15 (context param on ragFeed, ragSearch) |
| `server/services/feed.ts` | MODIFY | +20 (build context, pass to ragFeed) |
| `server/routes/user.ts` | MODIFY | +15 (location in settings schema + handler) |
| `app/onboarding/onboarding-context.tsx` | MODIFY | +10 (location fields + step reconfiguration) |
| **`app/onboarding/location/page.tsx`** | **NEW** | ~150 (location capture step) |
| `app/(main)/profile/settings/page.tsx` | MODIFY | +30 (location section) |
| `rag_pipeline/orchestrator/profile_enrichment.py` | MODIFY | +30 (context enrichment) |
| `rag_pipeline/orchestrator/orchestrator.py` | MODIFY | +50 (query augmentation + re-ranking) |
| SQL migration | NEW | ~5 (ALTER TABLE households) |
