# PRD-08: Ingredient Intelligence Panel

> **Tech Stack:** Next.js (frontend), Express + Drizzle ORM (backend), FastAPI (RAG API), Neo4j 5 (graph DB), PostgreSQL `gold` schema
> **Auth:** Appwrite (B2B auth) → Express validates JWT → Express calls RAG API with `X-API-Key`
> **Repos:** `nutrib2b-v20` (frontend), `nutriapp-backend` (backend), `rag-pipeline-hybrid-reterival` (RAG API)
> **Depends On:** PRD-01 (Foundation & RAG Infra)

---

## 8.1 Overview

Enrich the **Product Detail view** with an **Ingredient Intelligence Panel** that auto-generates ingredient analysis, allergen badges, diet compatibility flags, nutrition density scoring, and customer suitability summaries.

**Why this matters:** Product pages currently show basic nutrition data (calories, protein, fat, carbs, fiber, sugar, sodium) and a quality grade. Vendors have no way to see at a glance which diets this product is compatible with, which allergens it contains, or how many of their customers it can be safely recommended to.

**Current State:**

- Frontend: `products/page.tsx` shows products in table/card views with inline nutrition and quality grade. Extensible.
- Backend: `GET /products/:id` returns product data. `GET /api/quality/products/:id` returns quality scores. No ingredient analysis endpoint.
- Database: `product_ingredients` (with quantity, order, is_primary), `ingredient_allergens` (with threshold_ppm), `ingredients` tables all exist.
- RAG: Existing intents `get_nutritional_info` and `check_diet_compliance` in `cypher_query_generator.py`.

## 8.2 User Stories

| ID | Story | Priority |
|----|-------|----------|
| II-1 | As a vendor admin, I see an ingredient list with primary flags on product detail | P0 |
| II-2 | As a vendor admin, I see allergen badges (Contains / May Contain / Free From) | P0 |
| II-3 | As a vendor admin, I see diet compatibility flags (Keto ✅, Vegan ❌, etc.) | P0 |
| II-4 | As a vendor admin, I see a nutrition density score (0-10) | P1 |
| II-5 | As a vendor admin, I see customer suitability summary (safe for X%, caution for Y%) | P1 |
| II-6 | As a vendor admin, I see quality warnings if data is incomplete | P0 |

## 8.3 Technical Architecture

### 8.3.1 Backend API

#### [NEW] Route in `server/routes/products.ts` (extend existing) or `server/routes/intelligence.ts`

```typescript
// GET /api/v1/products/:id/intelligence
router.get("/products/:id/intelligence", requireAuth, async (req, res) => {
  const { id } = req.params;
  const vendorId = req.vendorId;

  // 1. SQL: ingredient list
  const ingredients = await db.execute(sql`
    SELECT i.name, pi.quantity, pi.unit, pi.is_primary, pi.ingredient_order
    FROM gold.product_ingredients pi
    JOIN gold.ingredients i ON pi.ingredient_id = i.id
    WHERE pi.product_id = ${id}
    ORDER BY pi.ingredient_order ASC
  `);

  // 2. SQL: allergens via ingredient path
  const allergens = await db.execute(sql`
    SELECT DISTINCT a.name, a.code, ia.threshold_ppm,
           'ingredient_analysis' AS source
    FROM gold.product_ingredients pi
    JOIN gold.ingredient_allergens ia ON ia.ingredient_id = pi.ingredient_id
    JOIN gold.allergens a ON ia.allergen_id = a.id
    WHERE pi.product_id = ${id}
  `);

  // 3. SQL: quality warnings
  const quality = await db.execute(sql`
    SELECT * FROM gold.product_quality_scores WHERE product_id = ${id}
  `);

  // 4. SQL: customer suitability (count safe/caution/unsafe)
  const suitability = await db.execute(sql`
    WITH customer_allergens AS (
      SELECT ca.b2b_customer_id, ARRAY_AGG(a.code) AS allergen_codes
      FROM gold.b2b_customer_allergens ca
      JOIN gold.allergens a ON ca.allergen_id = a.id
      JOIN gold.b2b_customers c ON ca.b2b_customer_id = c.id
      WHERE c.vendor_id = ${vendorId}
      GROUP BY ca.b2b_customer_id
    ),
    product_allergens AS (
      SELECT ARRAY_AGG(DISTINCT a.code) AS allergen_codes
      FROM gold.product_ingredients pi
      JOIN gold.ingredient_allergens ia ON ia.ingredient_id = pi.ingredient_id
      JOIN gold.allergens a ON ia.allergen_id = a.id
      WHERE pi.product_id = ${id}
    )
    SELECT
      COUNT(*) FILTER (WHERE NOT pa.allergen_codes && ca.allergen_codes) AS safe_count,
      COUNT(*) FILTER (WHERE pa.allergen_codes && ca.allergen_codes) AS unsafe_count,
      COUNT(*) AS total
    FROM customer_allergens ca, product_allergens pa
  `);

  // 5. RAG: diet compatibility (optional — uses graph traversal)
  let dietCompatibility = [];
  const ragResult = await ragProductIntel({ product_id: id, vendor_id: vendorId });
  if (ragResult?.diet_compatibility) {
    dietCompatibility = ragResult.diet_compatibility;
  }

  res.json({
    ingredient_count: ingredients.rows.length,
    ingredients: ingredients.rows,
    allergens: allergens.rows,
    diet_compatibility: dietCompatibility,
    customer_suitability: suitability.rows[0] || { safe_count: 0, unsafe_count: 0, total: 0 },
    quality_warnings: extractWarnings(quality.rows[0]),
  });
});
```

### 8.3.2 Frontend Changes

#### [MODIFY] `app/products/page.tsx` or Product Detail Modal

```
Product Detail (Enhanced)
├── Basic Info (existing — name, brand, SKU, category)
├── Nutrition Panel (existing — inline calorie/macro values)
├── 🆕 Ingredient Intelligence Tab
│   ├── Ingredient List
│   │   ├── Each ingredient with quantity, unit
│   │   └── Primary ingredient badge (⭐)
│   ├── Allergen Badges
│   │   ├── 🔴 "Contains: Peanut, Dairy"
│   │   ├── 🟡 "May Contain: Soy" (if threshold_ppm is low)
│   │   └── ✅ "Free From: Gluten, Shellfish"
│   ├── Diet Compatibility Matrix
│   │   ├── Keto ✅ / Vegan ❌ / Gluten-Free ✅ / Paleo 🟡
│   │   └── Hover shows reason (e.g., "Contains milk protein")
│   ├── Customer Suitability Summary
│   │   └── Pie: "Safe for 85% | Caution 10% | Unsafe 5%"
│   └── Quality Warnings
│       └── ⚠️ "Missing allergen declaration for soy"
└── Quality Score (existing)
```

---

## 8.RAG — RAG Team Scope

> **Owner:** RAG Pipeline Engineer

### Deliverables

#### 1. `POST /b2b/product-intel` Endpoint

**Request:**

```json
{
  "product_id": "uuid",
  "vendor_id": "uuid"
}
```

**Response:**

```json
{
  "diet_compatibility": [
    { "diet": "Keto", "compatible": true, "reason": null },
    { "diet": "Vegan", "compatible": false, "reason": "Contains milk protein (whey)" },
    { "diet": "Gluten-Free", "compatible": true, "reason": null },
    { "diet": "Paleo", "compatible": false, "reason": "Contains refined sugar" }
  ]
}
```

#### 2. Cypher for Diet Compatibility

```cypher
MATCH (p:Product {id: $product_id})-[:SOLD_BY]->(v:Vendor {id: $vendor_id})
MATCH (d:DietaryPreference)
OPTIONAL MATCH (p)-[:COMPATIBLE_WITH_DIET]->(d)
OPTIONAL MATCH (p)-[:CONTAINS_INGREDIENT]->(i:Ingredient)<-[:DIET_FORBIDS]-(d)
WITH d,
     CASE 
       WHEN p IS NOT NULL THEN true      // Direct compatibility flag
       WHEN i IS NULL THEN true           // No forbidden ingredients
       ELSE false
     END AS compatible,
     COLLECT(DISTINCT i.name) AS forbidden_ingredients
RETURN d.name AS diet, compatible,
       CASE WHEN SIZE(forbidden_ingredients) > 0
            THEN 'Contains ' + forbidden_ingredients[0]
            ELSE null
       END AS reason
```

---

## 8.4 Acceptance Criteria

- [ ] `GET /api/v1/products/:id/intelligence` returns ingredient analysis
- [ ] Ingredient list shows with quantity, unit, and primary flag
- [ ] Allergen badges classify as Contains / May Contain / Free From
- [ ] Diet compatibility shows when RAG available
- [ ] Customer suitability chart shows safe/caution/unsafe percentages
- [ ] Quality warnings display from existing quality scores

## 8.5 Route Registration

```typescript
// Extends existing products.ts or new intelligence.ts
```

## 8.6 Environment Variables

```env
USE_GRAPH_INTEL=false  # Set to 'true' to enable diet compatibility via graph
```
