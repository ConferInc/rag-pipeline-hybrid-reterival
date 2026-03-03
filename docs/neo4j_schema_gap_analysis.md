# Neo4j Schema Gap Analysis
**Date:** 2026-02-26  
**Purpose:** Compare current Neo4j schema against all PRD requirements (PRD-09 through PRD-17).

---

## Current Neo4j State

### Nodes (14 present)
```
Ingredient, NutritionValue, NutrientDefinition, NutritionCategory,
Recipe, Cuisine, Product, B2C_Customer,
B2C_Customer_Health_Conditions, B2C_Customer_Health_Profiles,
Allergens, Dietary_Preferences, Certificates, Household
```

### Relationships (10 present)
```
HAS_NUTRITION, OF_NUTRIENT, PARENT_OF, BELONGS_TO_CUSINE,
HAS_PROFILE, HAS_CONDITION, IS_ALLERGIC, FOLLOWS_DIET,
VIEWED, SAVED
```

---

## Gap Analysis

### NODES

| Node | Required By | Status | Notes |
|------|-------------|--------|-------|
| `Recipe` | All PRDs | ✅ Present | |
| `Ingredient` | All PRDs | ✅ Present | |
| `Cuisine` | PRD-10, 12, 15 | ✅ Present | |
| `Product` | PRD-13, 14 | ✅ Present | |
| `B2C_Customer` | All PRDs | ✅ Present | |
| `Allergens` | PRD-10, 13, 14 | ✅ Present | |
| `Dietary_Preferences` | PRD-10, 11 | ✅ Present | |
| `Household` | PRD-09, 12 | ✅ Present | |
| `NutritionValue` | PRD-10, 11 | ✅ Present | |
| `NutrientDefinition` | PRD-10 | ✅ Present | |
| `NutritionCategory` | PRD-10 | ✅ Present | |
| `B2C_Customer_Health_Profiles` | PRD-11, 12 | ✅ Present | Used for nutritional gap scoring |
| `B2C_Customer_Health_Conditions` | PRD-09 | ✅ Present | |
| `Certificates` | — | ✅ Present | Not referenced in PRDs |
| `MealPlan` | PRD-12, 09 | ❌ MISSING | Meal plan generation |
| `MealPlanItem` | PRD-12, 09 | ❌ MISSING | Individual meals in a plan |
| `MealLog` | PRD-15, 12, 09 | ❌ MISSING | Daily meal logging |
| `MealLogItem` | PRD-15, 09 | ❌ MISSING | Individual items in a log |
| `ShoppingList` | PRD-13, 09 | ❌ MISSING | Grocery list |
| `ShoppingListItem` | PRD-13, 09 | ❌ MISSING | Items in a grocery list |
| `RecipeRating` | PRD-17, 09 | ❌ MISSING | Ratings for collaborative filtering |
| `ScanEvent` | PRD-14, 09 | ❌ MISSING | Scanner history |
| `HouseholdMember` | PRD-12, 09 | ❌ MISSING | Individual family members |
| `HouseholdBudget` | PRD-13, 09 | ❌ MISSING | Budget for grocery planning |
| `HealthProfile` | PRD-11, 12, 09 | ❌ MISSING | Per-member nutrition targets (calorie/protein/carb/fat goals) |
| `MealLogTemplate` | PRD-15, 09 | ❌ MISSING | Saved meal templates |
| `MealLogStreak` | PRD-15, 09 | ❌ MISSING | Streak tracking |

**Summary: 14 present, 13 missing**

---

### RELATIONSHIPS

| Relationship | Pattern | Required By | Status | Notes |
|-------------|---------|-------------|--------|-------|
| `HAS_NUTRITION` | `(Ingredient/Recipe)→(NutritionValue)` | PRD-10 | ✅ Present | |
| `OF_NUTRIENT` | `(NutritionValue)→(NutrientDefinition)` | PRD-10 | ✅ Present | |
| `PARENT_OF` | `(NutritionCategory)→(NutritionCategory)` | PRD-10 | ✅ Present | |
| `BELONGS_TO_CUSINE` | `(Recipe)→(Cuisine)` | PRD-10, 12, 15 | ✅ Present | Note: typo in name ("CUSINE") — keep as-is to match existing data |
| `HAS_PROFILE` | `(B2C_Customer)→(B2C_Customer_Health_Profiles)` | PRD-09 | ✅ Present | |
| `HAS_CONDITION` | `(B2C_Customer)→(B2C_Customer_Health_Conditions)` | PRD-09 | ✅ Present | |
| `IS_ALLERGIC` | `(B2C_Customer)→(Allergens)` | PRD-10, 13, 14 | ✅ Present | PRDs call this `ALLERGIC_TO` — functionally equivalent |
| `FOLLOWS_DIET` | `(B2C_Customer)→(Dietary_Preferences)` | PRD-10, 11 | ✅ Present | |
| `VIEWED` | `(B2C_Customer)→(Recipe)` | PRD-11, 17 | ✅ Present | |
| `SAVED` | `(B2C_Customer)→(Recipe)` | PRD-11, 17 | ✅ Present | |
| `USES_INGREDIENT` | `(Recipe)→(Ingredient)` | PRD-10, 12, 15 | ✅ Present | Assumed present from existing Cypher queries |
| `RATED` | `(B2C_Customer)→(Recipe)` | PRD-17, 09 | ❌ MISSING | Core signal for collaborative filtering |
| `SCANNED` | `(B2C_Customer)→(Product)` | PRD-14, 09 | ❌ MISSING | Scanner history |
| `BELONGS_TO_HOUSEHOLD` | `(B2C_Customer)→(Household)` | PRD-12, 09 | ❌ MISSING | Links customer to their household |
| `HAS_MEMBER` | `(Household)→(HouseholdMember)` | PRD-12, 09 | ❌ MISSING | Household family members |
| `HAS_BUDGET` | `(Household)→(HouseholdBudget)` | PRD-13, 09 | ❌ MISSING | Household grocery budget |
| `HAS_PLAN` | `(B2C_Customer)→(MealPlan)` | PRD-12, 09 | ❌ MISSING | Customer's meal plans |
| `CONTAINS_ITEM` | `(MealPlan/MealLog/ShoppingList)→(Item)` | PRD-12, 15, 13 | ❌ MISSING | Items within plans/logs/lists |
| `PLANS_RECIPE` | `(MealPlanItem)→(Recipe)` | PRD-12, 09 | ❌ MISSING | Planned recipe in a meal slot |
| `LOGGED_MEAL` | `(B2C_Customer)→(MealLog)` | PRD-15, 12, 09 | ❌ MISSING | Critical for meal pattern analysis |
| `OF_RECIPE` | `(MealLogItem)→(Recipe)` | PRD-15, 09 | ❌ MISSING | Links logged meal to recipe |
| `OF_PRODUCT` | `(MealLogItem/ShoppingListItem)→(Product)` | PRD-15, 13 | ❌ MISSING | Links logged item to product |
| `HAS_LIST` | `(B2C_Customer)→(ShoppingList)` | PRD-13, 09 | ❌ MISSING | Customer's grocery lists |
| `DERIVED_FROM` | `(ShoppingList)→(MealPlan)` | PRD-13, 09 | ❌ MISSING | List generated from which plan |
| `CAN_SUBSTITUTE` | `(Product)→(Product)` | PRD-13, 14 | ❌ MISSING | Product substitution graph |
| `CONTAINS_INGREDIENT` | `(Product)→(Ingredient)` | PRD-13 | ❌ MISSING | For allergen-safe product matching |

**Summary: 11 present, 15 missing**

---

## Priority Classification

### P0 — Required for Phase 2 exit criteria (B2C data sync)
These are needed for the sync script to work and for basic personalization:

**Missing Nodes:**
- `HealthProfile` — per-member nutrition targets (calorie/protein/carb/fat goals)
- `HouseholdMember` — individual family members with their own allergens/diets
- `MealLog` + `MealLogItem` — meal history (needed for variety scoring in PRD-12/15)
- `RecipeRating` — ratings data (needed for collaborative filtering in PRD-17)
- `ScanEvent` — scanner history

**Missing Relationships:**
- `RATED` — `(B2C_Customer)→(Recipe)` — core collaborative filtering signal
- `SCANNED` — `(B2C_Customer)→(Product)`
- `BELONGS_TO_HOUSEHOLD` — `(B2C_Customer)→(Household)`
- `HAS_MEMBER` — `(Household)→(HouseholdMember)`
- `LOGGED_MEAL` — `(B2C_Customer)→(MealLog)`
- `OF_RECIPE` — `(MealLogItem)→(Recipe)`
- `CONTAINS_INGREDIENT` — `(Product)→(Ingredient)` — for allergen-safe product matching

### P1 — Required for meal planning and grocery features (PRD-12, 13)
**Missing Nodes:** `MealPlan`, `MealPlanItem`, `ShoppingList`, `ShoppingListItem`, `HouseholdBudget`

**Missing Relationships:** `HAS_PLAN`, `CONTAINS_ITEM`, `PLANS_RECIPE`, `HAS_LIST`, `DERIVED_FROM`, `HAS_BUDGET`, `CAN_SUBSTITUTE`, `OF_PRODUCT`

### P2 — Nice to have (PRD-15 full pattern analysis)
**Missing Nodes:** `MealLogTemplate`, `MealLogStreak`

---

## What's Already Covered (No Work Needed)

The Phase 2 exit criteria state:
> *"B2CCustomer nodes and ALLERGIC_TO / FOLLOWS_DIET relationships in Neo4j"*

✅ `B2C_Customer` nodes — **already present**  
✅ `IS_ALLERGIC` (equivalent to `ALLERGIC_TO`) — **already present**  
✅ `FOLLOWS_DIET` — **already present**  

This means the **minimum Phase 2 exit criteria are already met** by your existing Neo4j data.  
The sync script (Step 2.3) just needs to keep these in sync with PostgreSQL going forward.

---

## Next Steps

| Step | Action | Needed For |
|------|--------|-----------|
| **2.2** | Create constraints + indexes for the 13 missing node types | Before first sync of new tables |
| **2.3 P0** | Sync `b2c_customers`, `b2c_customer_allergens`, `b2c_customer_dietary_preferences`, `b2c_customer_health_profiles` | Phase 2 exit criteria |
| **2.3 P1** | Sync `recipes`, `recipe_ingredients`, `products` | PRD-10, 13, 14 |
| **2.3 P2** | Sync `meal_logs`, `meal_plan_items`, `customer_product_interactions` | PRD-12, 15, 17 |
| **Add `RATED` rel** | Sync `recipe_ratings` → `(B2C_Customer)-[:RATED]->(Recipe)` | PRD-17 collaborative filtering |
| **Add `CONTAINS_INGREDIENT`** | Sync `product_ingredients` → `(Product)-[:CONTAINS_INGREDIENT]->(Ingredient)` | PRD-13 allergen-safe grocery |
| **Add `CAN_SUBSTITUTE`** | Populate product substitution edges | PRD-13, 14 |
