# PRD 34: USDA 2025 Food Pyramid Integration

> **Tech Stack:** Next.js (frontend), Express + Drizzle ORM (backend), FastAPI (RAG API), Neo4j 5 (graph DB), PostgreSQL `gold` schema, LiteLLM proxy → OpenAI models  
> **Auth:** Appwrite (B2C auth)  
> **Repos:** `nutrib2c-frontend` (frontend), `nutrition-backend-b2c` (backend), `rag-pipeline-hybrid-reterival` (RAG API)  
> **Family Context:** All features support household/family member switching. The `households` table is the root entity; each member is a `b2c_customers` row linked via `household_id`.  
> **Depends On:** PRD-33 (Contextual Recommendations — provides calorie/macro context), PRD-12 (Graph Meal Planning), PRD-04 (AI Meal Planner)

---

## 34.1 Overview

Integrate the **USDA 2025-2030 Dietary Guidelines** (Inverted Food Pyramid) into meal recommendations, meal plan generation, and nutritional analysis. The goal is to ensure all AI-generated meal plans and recommendations adhere to evidence-based food group proportions — not just calorie/macro targets.

**Vijay Sir's Directive:**
> _"When you generate recommendation, do you also refer to the food pyramid? USDA food and nutritional supplement program?"_ — (26:53–27:12)

**Model Selected:** USDA 2025 Inverted Food Pyramid — the latest federal guidelines released January 2026.

**Current State:**

- `generator.py` — generic LLM wrapper with no nutritional model in system prompt
- `mealPlanLLM.ts` — system prompt has calorie/macro constraints but **no food group balance**
- No `nutritional_guidelines` reference data exists in the database
- Nutrition dashboard shows macros (protein/carbs/fat) but **no food group tracking**

**Target State:**

- USDA 2025 guidelines injected into RAG prompts and meal plan LLM prompts
- Meal plans validated against food group proportions after generation
- Reference table for nutritional guidelines (extensible for other models)
- Nutrition dashboard shows food group balance (optional UI enhancement)

## 34.2 User Stories

| ID | Story | Priority |
|----|-------|----------|
| FP-1 | As a user, my AI meal plan follows USDA 2025 food group proportions (protein, dairy, vegetables, fruits, grains) | P0 |
| FP-2 | As a user, I see a warning if my meal plan is deficient in any food group | P1 |
| FP-3 | As a user, recommendations include a variety of food groups rather than repeating the same group | P0 |
| FP-4 | As a user, the grocery list generated from my meal plan reflects balanced food group distribution | P1 |
| FP-5 | As a product owner, I can update nutritional guidelines in the database without code changes | P1 |

## 34.3 Technical Architecture

### 34.3.1 USDA 2025 Daily Targets (Reference Data)

Based on the 2025-2030 Dietary Guidelines for Americans, for a standard 2,000-calorie intake:

| Food Group | Daily Target | Unit | Calories Allotment | Priority (Inverted Pyramid) |
|-----------|-------------|------|-------------------|----------------------------|
| Protein | 5.5-6.5 oz | oz-eq/day | ~35% | 1 (top — highest priority) |
| Dairy (full-fat) | 3 servings | cup-eq/day | ~15% | 2 |
| Vegetables | 2.5 cups | cup-eq/day | ~20% | 3 |
| Fruits | 2 cups | cup-eq/day | ~10% | 4 |
| Whole Grains | 6 oz | oz-eq/day | ~20% | 5 (bottom — least) |

**Soft guidelines (advisory — not enforced as hard constraints):**
- Added sugars: aim for < 10g per meal, ideally zero
- Sodium: aim for < 2,300 mg/day  
- Saturated fat: aim for < 10% of daily calories
- Processed foods: minimize where possible

### 34.3.2 Schema Changes

#### [NEW] Reference Table — `gold.nutritional_guidelines`

```sql
CREATE TABLE gold.nutritional_guidelines (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  model_name VARCHAR(50) NOT NULL,         -- 'usda_2025'
  food_group VARCHAR(100) NOT NULL,        -- 'protein' | 'dairy' | 'vegetables' | 'fruits' | 'whole_grains'
  daily_target_min NUMERIC(10,2),          -- Minimum daily target
  daily_target_max NUMERIC(10,2),          -- Maximum daily target
  daily_target_unit VARCHAR(20) NOT NULL,  -- 'oz_eq' | 'cup_eq' | 'servings' | 'g' | 'mg'
  calorie_percentage NUMERIC(5,2),         -- % of daily calories
  pyramid_priority INTEGER,                -- 1=highest (top), 5=lowest (bottom)
  calorie_basis INTEGER DEFAULT 2000,      -- Reference calorie level
  scaling_factor NUMERIC(5,3) DEFAULT 1.0, -- Scale for different calorie levels
  notes TEXT,
  is_active BOOLEAN DEFAULT true,
  created_at TIMESTAMP DEFAULT NOW()
);

-- Seed data for USDA 2025
INSERT INTO gold.nutritional_guidelines 
  (model_name, food_group, daily_target_min, daily_target_max, daily_target_unit, calorie_percentage, pyramid_priority) 
VALUES
  ('usda_2025', 'protein',       5.5, 6.5,  'oz_eq',   35, 1),
  ('usda_2025', 'dairy',         3.0, 3.0,  'cup_eq',  15, 2),
  ('usda_2025', 'vegetables',    2.5, 3.0,  'cup_eq',  20, 3),
  ('usda_2025', 'fruits',        1.5, 2.0,  'cup_eq',  10, 4),
  ('usda_2025', 'whole_grains',  5.0, 6.0,  'oz_eq',   20, 5);

-- Soft guidelines (advisory thresholds — logged as warnings, not enforced)
CREATE TABLE gold.nutritional_soft_guidelines (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  model_name VARCHAR(50) NOT NULL,
  nutrient VARCHAR(50) NOT NULL,        -- 'added_sugar_per_meal' | 'sodium_daily' | 'saturated_fat_pct'
  recommended_max NUMERIC(10,2) NOT NULL,
  unit VARCHAR(20) NOT NULL,            -- 'g' | 'mg' | 'percent'
  severity VARCHAR(10) DEFAULT 'info',  -- 'info' | 'warning' — for UI display
  is_active BOOLEAN DEFAULT true
);

INSERT INTO gold.nutritional_soft_guidelines (model_name, nutrient, recommended_max, unit, severity) VALUES
  ('usda_2025', 'added_sugar_per_meal', 10, 'g', 'info'),
  ('usda_2025', 'sodium_daily', 2300, 'mg', 'info'),
  ('usda_2025', 'saturated_fat_daily_pct', 10, 'percent', 'info');
```

### 34.3.3 Backend Changes

#### [NEW] `server/services/foodPyramidValidator.ts`

Post-generation validation for meal plans:

```typescript
interface FoodGroupAudit {
  group: string;
  targetMin: number;
  targetMax: number;
  unit: string;
  actual: number;
  status: "adequate" | "below" | "above";
}

/**
 * Validate a daily meal plan against USDA 2025 guidelines.
 * Called after LLM generates a meal plan — log warnings if deficient.
 */
export async function auditMealPlanAgainstGuidelines(
  dailyMeals: MealPlanDay,
  userCalorieTarget: number
): Promise<FoodGroupAudit[]> {
  // 1. Load guidelines from DB
  const guidelines = await executeRaw(
    `SELECT * FROM gold.nutritional_guidelines WHERE model_name = 'usda_2025' AND is_active = true`
  );

  // 2. Scale targets to user's calorie level
  const scaleFactor = userCalorieTarget / 2000;

  // 3. Aggregate food groups from the meal plan recipes
  const dailyGroups = aggregateFoodGroups(dailyMeals);

  // 4. Compare and return audit
  return guidelines.map((g: any) => {
    const scaledMin = g.daily_target_min * scaleFactor;
    const scaledMax = g.daily_target_max * scaleFactor;
    const actual = dailyGroups[g.food_group] ?? 0;

    return {
      group: g.food_group,
      targetMin: scaledMin,
      targetMax: scaledMax,
      unit: g.daily_target_unit,
      actual,
      status: actual < scaledMin ? "below" : actual > scaledMax ? "above" : "adequate",
    };
  });
}
```

#### [MODIFY] `server/services/mealPlanLLM.ts` — Add USDA Guidelines to System Prompt

```diff
  const systemPrompt = `You are a nutrition-aware meal planning assistant.
  
+ NUTRITIONAL GUIDELINES (USDA 2025 Inverted Food Pyramid):
+ When creating daily meal plans, ensure balanced distribution across food groups:
+ 1. PROTEIN (highest priority): ${proteinTarget}g/day from varied sources (poultry, fish, beans, eggs, nuts)
+ 2. DAIRY: 3 servings/day, full-fat preferred
+ 3. VEGETABLES: 2.5-3 cups/day, variety of colors
+ 4. FRUITS: 1.5-2 cups/day, whole fruits preferred
+ 5. WHOLE GRAINS: 5-6 oz/day, minimize refined grains
+
+ Soft guidelines per meal (aim for, not enforced):
+ - Added sugars: aim for <10g per meal
+ - Minimize ultra-processed foods where possible
+ - Try to include protein at every meal
+
  ${existingConstraints}
  `;
```

#### [MODIFY] `server/services/mealPlan.ts` — Post-generation Audit

```diff
+ import { auditMealPlanAgainstGuidelines } from "./foodPyramidValidator.js";

  // After LLM generates meal plan:
+ const audit = await auditMealPlanAgainstGuidelines(generatedPlan, userCalorieTarget);
+ const deficiencies = audit.filter(a => a.status === "below");
+ if (deficiencies.length > 0) {
+   console.warn("[MealPlan] Food group deficiencies:", deficiencies);
+   // Optionally: regenerate with stronger constraints, or add warning to response
+ }
```

### 34.3.4 Frontend Changes

No critical frontend changes for initial implementation. The food pyramid guidelines are primarily enforced via prompt engineering and backend validation.

**Optional P2 enhancement:** Add a "Food Group Balance" section to the nutrition dashboard showing daily/weekly food group distribution.

| Element | Priority | Implementation |
|---------|----------|---------------|
| Food group balance bar chart on nutrition page | P2 | `<BarChart>` showing daily intake vs target per food group |
| "Balanced ✓" / "Low Protein ⚠️" badges on meal plan | P2 | Badge component based on audit results |

## 34.RAG — RAG Team Scope

> **Repo:** `rag-pipeline-hybrid-reterival`  
> **Owner:** RAG Pipeline Engineer  
> **The B2C team does NOT touch these files.**

### Deliverables

#### 1. System Prompt — Inject USDA 2025 Guidelines

**File:** `rag_pipeline/generation/generator.py`

When generating feed responses, chatbot answers, or meal suggestions, include food pyramid context:

```python
USDA_2025_SYSTEM_CONTEXT = """
Follow USDA 2025-2030 Dietary Guidelines (Inverted Food Pyramid) when recommending meals:

FOOD GROUP PRIORITIES (highest to lowest):
1. PROTEIN: Include at every meal. 1.2-1.6 g/kg body weight/day. Varied sources.
2. DAIRY: 3 servings/day, full-fat encouraged.
3. VEGETABLES: 2.5+ cups/day, rainbow variety, leafy greens.
4. FRUITS: 1.5-2 cups/day, whole fruits over juice.
5. WHOLE GRAINS: 5-6 oz/day, minimize refined carbs.

SOFT GUIDELINES (advisory — gently steer toward these, do not reject meals that exceed them):
- Added sugars: aim for <10g per meal
- Sodium: aim for <2,300 mg/day total
- Processed foods: minimize where possible
- Non-nutritive sweeteners: minimize where possible

When suggesting meals, ensure each meal contributes to balanced food group coverage.
Do NOT suggest meals that are purely one food group (e.g., all carbs, no protein).
"""

def build_system_prompt(entities: dict, include_guidelines: bool = True) -> str:
    prompt = BASE_SYSTEM_PROMPT
    if include_guidelines:
        prompt += "\n\n" + USDA_2025_SYSTEM_CONTEXT
    # ... existing context injection (from PRD-33)
    return prompt
```

#### 2. Recipe Scoring — Food Group Balance Bonus

When ranking recipes in feed/search results, apply a food group diversity bonus:

```python
def food_group_balance_score(recipe: dict) -> float:
    """
    Score a recipe based on how many food groups it covers.
    Recipes covering 3+ food groups get a bonus.
    """
    groups_present = set()
    
    # Detect food groups from recipe metadata
    if recipe.get("protein_g", 0) > 10:
        groups_present.add("protein")
    if recipe.get("has_dairy"):
        groups_present.add("dairy")
    if recipe.get("has_vegetables") or recipe.get("vegetable_servings", 0) > 0:
        groups_present.add("vegetables")
    if recipe.get("has_fruit") or recipe.get("fruit_servings", 0) > 0:
        groups_present.add("fruits")
    if recipe.get("has_whole_grains"):
        groups_present.add("whole_grains")
    
    coverage = len(groups_present) / 5.0
    return 0.8 + (coverage * 0.4)  # Range: 0.8 (0 groups) to 1.2 (all 5)
```

#### 3. Feed Response — Include Food Group Tags

In feed/recommendation responses, tag each recipe with its primary food groups:

```json
{
  "recipes": [
    {
      "id": "uuid",
      "title": "Grilled Chicken Salad",
      "score": 0.92,
      "food_groups": ["protein", "vegetables"],
      "food_group_coverage": 0.4
    }
  ]
}
```

This allows the B2C frontend to optionally show food group badges on recipe cards.

## 34.4 Acceptance Criteria

- [ ] USDA 2025 guidelines are stored in `gold.nutritional_guidelines` reference table
- [ ] Soft guidelines are stored in `gold.nutritional_soft_guidelines` reference table
- [ ] Meal plan LLM system prompt includes USDA 2025 food group requirements
- [ ] Generated meal plans include protein source at every meal
- [ ] Post-generation audit identifies food group deficiencies (logged as warnings)
- [ ] RAG feed recommendations favor recipes covering multiple food groups
- [ ] System prompt includes soft guidelines (added sugar, sodium, processed foods) as advisory
- [ ] Guidelines scale appropriately for non-2000-calorie targets
- [ ] Reference data is editable in DB without code changes

## 34.5 Environment Variables

```env
# No new env vars needed — guidelines are data-driven from gold.nutritional_guidelines
# The model_name 'usda_2025' is used as the Active model
```

## 34.6 Files Summary

| File | Action | Lines (est.) |
|------|--------|-------------|
| SQL migration (guidelines + soft_guidelines tables + seed) | **NEW** | ~50 |
| `shared/goldSchema.ts` | MODIFY | +20 (2 new table definitions) |
| **`server/services/foodPyramidValidator.ts`** | **NEW** | ~80 (audit logic) |
| `server/services/mealPlanLLM.ts` | MODIFY | +20 (USDA prompt injection) |
| `server/services/mealPlan.ts` | MODIFY | +15 (post-generation audit) |
| `rag_pipeline/generation/generator.py` | MODIFY | +25 (USDA system context) |
| `rag_pipeline/orchestrator/orchestrator.py` | MODIFY | +20 (food group scoring) |
