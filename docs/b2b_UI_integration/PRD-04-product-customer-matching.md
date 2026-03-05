# PRD-04: Product-Customer Matching

> **Tech Stack:** Next.js (frontend), Express + Drizzle ORM (backend), FastAPI (RAG API), Neo4j 5 (graph DB), PostgreSQL `gold` schema
> **Auth:** Appwrite (B2B auth) → Express validates JWT → Express calls RAG API with `X-API-Key`
> **Repos:** `nutrib2b-v20` (frontend), `nutriapp-backend` (backend), `rag-pipeline-hybrid-reterival` (RAG API)
> **Depends On:** PRD-01 (Foundation & RAG Infra)

---

## 4.1 Overview

The **inverse** of PRD-02: given a single product, find which of the vendor's customers it can be safely recommended to. On the **Products page** or product detail view, a vendor user clicks "Find Matching Customers" to see a ranked list of customers the product is safe and nutritionally aligned for.

**Why this matters:** Vendors need to know which customers they can market specific products to without risking allergen conflicts or health condition incompatibilities. This is critical for targeted marketing campaigns and safety compliance.

**Current State:**

- Frontend: Products page exists with table/card views, product detail modals. No customer matching feature.
- Backend: Product routes exist (`GET /products`, `GET /products/:id`). No matching endpoint.
- RAG Pipeline: The graph structure supports this — `Product→Ingredient→Allergen` and `B2BCustomer→Allergen` paths exist.

**SQL Fallback:** If RAG is down, the "Find Matching Customers" button shows "Matching unavailable" state.

## 4.2 User Stories

| ID | Story | Priority |
|----|-------|----------|
| PM-1 | As a vendor admin, I click "Find Matching Customers" on a product and see a ranked customer list | P0 |
| PM-2 | As a vendor admin, each matched customer shows safety indicators (green/yellow/red) | P0 |
| PM-3 | As a vendor admin, I see warnings for customers with mild intolerances (not hard excluded) | P1 |
| PM-4 | As a vendor admin, I can export the matching customer list as CSV | P1 |
| PM-5 | As a vendor admin, I see match reasons per customer ("No allergen conflicts", "Meets keto diet") | P0 |

## 4.3 Technical Architecture

### 4.3.1 Backend API

#### [MODIFY] `server/routes/recommendations.ts`

```typescript
// POST /api/v1/products/:id/matching-customers
router.post("/products/:id/matching-customers", requireAuth, async (req, res) => {
  const { id } = req.params;
  const vendorId = req.vendorId;
  const { limit = 50, includeWarnings = true } = req.body;

  const ragResult = await ragMatch({
    product_id: id,
    vendor_id: vendorId,
    limit,
    include_reasons: true,
    include_warnings: includeWarnings,
  });

  if (!ragResult) {
    return res.json({
      customers: [],
      summary: null,
      fallback: true,
      message: "Matching engine unavailable",
    });
  }

  res.json(ragResult);
});
```

### 4.3.2 Neo4j Cypher Query Pattern

```cypher
// Get product allergens
MATCH (p:Product {id: $product_id})-[:SOLD_BY]->(v:Vendor {id: $vendor_id})
OPTIONAL MATCH (p)-[:CONTAINS_INGREDIENT]->(i:Ingredient)-[:CONTAINS_ALLERGEN]->(pa:Allergen)
WITH p, v, COLLECT(DISTINCT pa.code) AS product_allergens

// Find all vendor customers
MATCH (c:B2BCustomer)-[:BELONGS_TO_VENDOR]->(v)
WHERE c.account_status = 'active'

// Get customer allergens with severity
OPTIONAL MATCH (c)-[ca:ALLERGIC_TO]->(a:Allergen)
WITH p, product_allergens, c,
     COLLECT({code: a.code, severity: ca.severity}) AS customer_allergen_details,
     COLLECT(DISTINCT a.code) AS customer_allergen_codes

// Classify: safe / warning / conflict
WITH c, product_allergens, customer_allergen_codes, customer_allergen_details,
     [x IN product_allergens WHERE x IN customer_allergen_codes] AS conflicts,
     [d IN customer_allergen_details 
      WHERE d.code IN product_allergens AND d.severity IN ['mild']] AS mild_conflicts

// Get customer diets for bonus scoring
OPTIONAL MATCH (c)-[:FOLLOWS_DIET]->(dp:DietaryPreference)
WITH c, conflicts, mild_conflicts, COLLECT(DISTINCT dp.name) AS customer_diets,
     CASE 
       WHEN SIZE(conflicts) = 0 THEN 'safe'
       WHEN SIZE(conflicts) = SIZE(mild_conflicts) THEN 'warning'
       ELSE 'conflict'
     END AS safety_status

WHERE safety_status IN ['safe', 'warning']

RETURN c.id, c.full_name, c.email,
       safety_status,
       customer_diets,
       CASE WHEN safety_status = 'safe' THEN 1.0 ELSE 0.5 END AS match_score
ORDER BY match_score DESC, c.full_name
LIMIT $limit
```

### 4.3.3 Frontend Changes

#### [MODIFY] `app/products/page.tsx`

Add a "Find Matching Customers" action button per product row:

```
Product Row Actions (Enhanced)
├── View Details (existing)
├── Edit (existing)
└── 🆕 "Find Matching Customers" button
    └── Opens side panel / modal:
        ├── Product summary bar (name, brand, allergens detected)
        ├── Customer list:
        │   ├── 🟢 Safe customers (no conflicts)
        │   ├── 🟡 Warning customers (mild intolerances)
        │   └── Match reasons per customer
        ├── Summary: "45 safe, 5 with warnings, 10 excluded"
        ├── 🆕 "Export CSV" button
        └── Fallback: "Matching engine unavailable"
```

#### [NEW] `components/products/CustomerMatchPanel.tsx`

---

## 4.RAG — RAG Team Scope

> **Repo:** `rag-pipeline-hybrid-reterival`
> **Owner:** RAG Pipeline Engineer

### Deliverables

#### 1. `POST /b2b/product-customers` Endpoint

**Request:**

```json
{
  "product_id": "uuid",
  "vendor_id": "uuid",
  "limit": 50,
  "include_reasons": true,
  "include_warnings": true
}
```

**Response:**

```json
{
  "customers": [
    {
      "customer_id": "uuid",
      "customer_name": "Jane Doe",
      "email": "jane@example.com",
      "match_score": 1.0,
      "safety_status": "safe",
      "reasons": ["No allergen conflicts", "Matches keto diet preference"],
      "warnings": [],
      "diets": ["Keto", "High-Protein"]
    },
    {
      "customer_id": "uuid2",
      "customer_name": "John Smith",
      "match_score": 0.5,
      "safety_status": "warning",
      "reasons": ["Mild lactose intolerance — product contains small amounts of dairy"],
      "warnings": ["Contains dairy — customer has mild lactose intolerance"],
      "diets": ["Vegetarian"]
    }
  ],
  "summary": {
    "total_customers": 100,
    "safe_count": 45,
    "warning_count": 5,
    "excluded_count": 10,
    "not_evaluated_count": 40
  },
  "retrieval_time_ms": 280
}
```

#### 2. Safety Classification Logic

| Severity | Allergen overlap | Classification |
|----------|-----------------|----------------|
| `anaphylactic` | Any overlap | ❌ EXCLUDED (never show) |
| `severe` | Any overlap | ❌ EXCLUDED |
| `moderate` | Any overlap | ⚠️ WARNING |
| `mild` | Any overlap | ⚠️ WARNING |
| No overlap | — | ✅ SAFE |

---

## 4.4 Acceptance Criteria

- [ ] `POST /api/v1/products/:id/matching-customers` returns customer list
- [ ] Customers with severe/anaphylactic allergen conflicts are EXCLUDED
- [ ] Customers with mild conflicts show as warnings (not excluded)
- [ ] Each customer has `safety_status`, `match_score`, and `reasons[]`
- [ ] Summary shows safe/warning/excluded counts
- [ ] CSV export downloads customer matching report
- [ ] Results are vendor-scoped (only show vendor's customers)

## 4.5 Route Registration

```typescript
// Reuses recommendations.ts router from PRD-02
```

## 4.6 Environment Variables

```env
USE_GRAPH_MATCH=false  # Set to 'true' to enable product-customer matching
```
