# PRD-07: Product-Customer Safety Engine

> **Tech Stack:** Next.js (frontend), Express + Drizzle ORM (backend), FastAPI (RAG API — optional for cross-reactivity), Neo4j 5 (graph DB), PostgreSQL `gold` schema
> **Auth:** Appwrite (B2B auth) → Express validates JWT
> **Repos:** `nutrib2b-v20` (frontend), `nutriapp-backend` (backend), `rag-pipeline-hybrid-reterival` (RAG API)
> **Depends On:** PRD-01 (Foundation & RAG Infra)

---

## 7.1 Overview

Extend the existing compliance engine to add **product-customer safety checking**: automatically cross-reference products against customer allergens and health conditions to find conflicts. Results flow into the existing alerts system.

**Key Distinction:** Current compliance checks are "data quality compliance" (does the product HAVE allergen declarations?). This PRD adds "health compliance" (is this product SAFE for specific customers?).

**Current State:**

- Backend: `compliance.ts` has `POST /compliance/run` evaluating rules (nutrition_completeness, allergen_declaration, image_quality). `alerts.ts` has `insertAlert()` with types (quality/compliance/ingestion/match/system).
- Frontend: Compliance page exists. Alerts page exists. Dashboard shows unread alerts count.
- Database: Junction tables `b2b_customer_allergens`, `product_ingredients`, `ingredient_allergens` all exist with `severity` columns.

**SQL Fallback:** Core safety check is SQL-based (no RAG dependency). RAG is optional for cross-reactivity detection.

## 7.2 User Stories

| ID | Story | Priority |
|----|-------|----------|
| SE-1 | As a vendor admin, I run a safety check and see all product-customer allergen conflicts | P0 |
| SE-2 | As a vendor admin, conflicts are ranked by severity (anaphylactic > severe > moderate > mild) | P0 |
| SE-3 | As a vendor admin, critical conflicts auto-create alerts | P0 |
| SE-4 | As a vendor admin, I see a safety summary (total conflicts, critical count, affected customers) | P0 |
| SE-5 | As a vendor admin, I can run safety check on demand or on a schedule | P1 |
| SE-6 | As a vendor admin, the safety check detects cross-reactive allergens via graph (e.g., peanut → tree nut) | P2 |

## 7.3 Technical Architecture

### 7.3.1 Backend API

#### [NEW] Route in `server/routes/compliance.ts` (extend existing)

```typescript
// POST /api/v1/compliance/safety-check
router.post("/compliance/safety-check", requireAuth, async (req, res) => {
  const vendorId = req.vendorId;

  // SQL-based direct allergen conflict detection
  const conflicts = await db.execute(sql`
    SELECT 
      p.id AS product_id, p.name AS product_name, p.brand,
      c.id AS customer_id, c.full_name AS customer_name,
      a.name AS conflict_allergen, a.code AS allergen_code,
      ca.severity AS customer_severity
    FROM gold.products p
    JOIN gold.product_ingredients pi ON pi.product_id = p.id
    JOIN gold.ingredient_allergens ia ON ia.ingredient_id = pi.ingredient_id
    JOIN gold.allergens a ON ia.allergen_id = a.id
    JOIN gold.b2b_customer_allergens ca ON ca.allergen_id = a.id
    JOIN gold.b2b_customers c ON ca.b2b_customer_id = c.id
    WHERE p.vendor_id = ${vendorId} AND c.vendor_id = ${vendorId}
    ORDER BY 
      CASE ca.severity 
        WHEN 'anaphylactic' THEN 1 
        WHEN 'severe' THEN 2 
        WHEN 'moderate' THEN 3 
        WHEN 'mild' THEN 4 
      END,
      c.full_name
  `);

  // Optional: RAG-enhanced cross-reactivity check
  let crossReactiveConflicts = [];
  const ragResult = await ragSafetyCheck({ vendor_id: vendorId });
  if (ragResult?.cross_reactive) {
    crossReactiveConflicts = ragResult.cross_reactive;
  }

  // Auto-create alerts for critical conflicts (anaphylactic/severe)
  const criticalConflicts = conflicts.rows.filter(
    c => c.customer_severity === 'anaphylactic' || c.customer_severity === 'severe'
  );
  for (const conflict of criticalConflicts.slice(0, 50)) {
    await insertAlert({
      vendor_id: vendorId,
      type: 'compliance',
      severity: 'critical',
      title: `Safety Alert: ${conflict.product_name} contains ${conflict.conflict_allergen}`,
      message: `Customer ${conflict.customer_name} has ${conflict.customer_severity} ${conflict.conflict_allergen} allergy. Product ${conflict.product_name} contains this allergen.`,
      metadata: { product_id: conflict.product_id, customer_id: conflict.customer_id },
    });
  }

  const summary = {
    total_conflicts: conflicts.rows.length,
    critical_count: criticalConflicts.length,
    affected_customers: new Set(conflicts.rows.map(c => c.customer_id)).size,
    affected_products: new Set(conflicts.rows.map(c => c.product_id)).size,
    cross_reactive_count: crossReactiveConflicts.length,
  };

  res.json({ conflicts: conflicts.rows, cross_reactive: crossReactiveConflicts, summary });
});
```

### 7.3.2 Frontend Changes

#### [MODIFY] `app/compliance/page.tsx`

Add a "Safety Check" tab alongside existing compliance views:

```
Compliance Page (Enhanced)
├── Rules Tab (existing)
├── Checks Tab (existing)
├── 🆕 Safety Check Tab
│   ├── "Run Safety Check" button
│   ├── Summary cards: Total conflicts, Critical, Affected customers, Affected products
│   ├── Conflict table:
│   │   ├── Severity badge (🔴 Anaphylactic / 🟠 Severe / 🟡 Moderate / 🟢 Mild)
│   │   ├── Product name
│   │   ├── Customer name
│   │   ├── Conflict allergen
│   │   └── Action: "View Product" / "View Customer"
│   └── Export CSV button
└── Summary Tab (existing)
```

---

## 7.RAG — RAG Team Scope

> **Owner:** RAG Pipeline Engineer
> **Scope:** Optional enhancement — cross-reactivity detection via graph

### Deliverables

#### 1. `POST /b2b/safety-check` Endpoint (Optional Enhancement)

The core safety check is SQL-based. The RAG endpoint adds cross-reactivity detection:

**Request:**

```json
{
  "vendor_id": "uuid",
  "product_ids": ["uuid1", "uuid2"],  // optional: scope to specific products
  "customer_ids": ["uuid3"]           // optional: scope to specific customers
}
```

**Response:**

```json
{
  "cross_reactive": [
    {
      "product_id": "uuid",
      "product_name": "Almond Butter",
      "customer_id": "uuid",
      "customer_name": "Jane Doe",
      "primary_allergen": "peanut",
      "cross_reactive_allergen": "tree_nut",
      "relationship": "Almonds (tree nut) cross-react with peanut allergy in ~30% of cases",
      "risk_level": "moderate"
    }
  ]
}
```

#### 2. Cross-Reactivity Cypher

```cypher
MATCH (c:B2BCustomer)-[:ALLERGIC_TO]->(ca:Allergen)
MATCH (p:Product)-[:SOLD_BY]->(v:Vendor {id: $vendor_id})
MATCH (p)-[:CONTAINS_INGREDIENT]->(i)-[:CONTAINS_ALLERGEN]->(pa:Allergen)
WHERE ca.code <> pa.code
  AND pa.code IN ca.cross_reactive_codes  // Cross-reactivity map
RETURN p.id, p.name, c.id, c.full_name, ca.name AS primary, pa.name AS cross_reactive
```

---

## 7.4 Acceptance Criteria

- [ ] `POST /api/v1/compliance/safety-check` returns all product-customer allergen conflicts
- [ ] Conflicts ranked by severity (anaphylactic first)
- [ ] Critical conflicts auto-create alerts via `insertAlert()`
- [ ] Summary shows total/critical/affected counts
- [ ] Safety Check tab renders on compliance page
- [ ] CSV export downloads conflict report
- [ ] (Optional) Cross-reactivity conflicts included when RAG available

## 7.5 Route Registration

```typescript
// Extends existing compliance.ts router
```

## 7.6 Environment Variables

```env
USE_GRAPH_SAFETY=false  # Optional: enables cross-reactivity detection via graph
```
