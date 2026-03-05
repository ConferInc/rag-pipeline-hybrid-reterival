# PRD-06: Health Analytics Widget

> **Tech Stack:** Next.js (frontend), Express + Drizzle ORM (backend), FastAPI (RAG API — optional), Neo4j 5 (graph DB — optional), PostgreSQL `gold` schema
> **Auth:** Appwrite (B2B auth) → Express validates JWT
> **Repos:** `nutrib2b-v20` (frontend), `nutriapp-backend` (backend), `rag-pipeline-hybrid-reterival` (RAG API — optional)
> **Depends On:** PRD-01 (Foundation & RAG Infra — only if RAG NLG insights are enabled)

---

## 6.1 Overview

Add a **Smart Health Analytics Widget** to the Dashboard page that visualizes the vendor's customer health data: allergen distribution, health condition breakdown, dietary preference coverage, and product-health gap analysis.

**Two implementation options:**

- **Option A (SQL-only):** Pure SQL aggregation + frontend visualization. No RAG dependency. Ships fast.
- **Option B (RAG-enhanced):** Adds an LLM-generated actionable insight card on top of Option A.

**Why this matters:** The dashboard currently shows 6 stat cards (total products, active customers, profiles with matches, pending jobs, catalog quality, unread alerts). There's no visualization of the customer base's health landscape — vendors can't see at a glance that 40% of their customers have nut allergies or that they have zero keto-compatible products.

**Current State:**

- Frontend: `dashboard/page.tsx` has 6 `StatCard` components, a Quick Actions card, and a Recent Activity placeholder.
- Backend: `GET /metrics` (consolidated stats), `GET /api/quality/vendor-summary`, `GET /api/alerts/summary`. No health analytics endpoint.
- Database: All health tables exist: `b2b_customer_allergens`, `b2b_customer_health_conditions`, `b2b_customer_dietary_preferences`, `b2b_customer_health_profiles`.

## 6.2 User Stories

| ID | Story | Priority |
|----|-------|----------|
| HA-1 | As a vendor admin, I see allergen distribution chart on the dashboard | P0 |
| HA-2 | As a vendor admin, I see health condition breakdown chart | P0 |
| HA-3 | As a vendor admin, I see top dietary preferences as badge pills | P0 |
| HA-4 | As a vendor admin, I see an AI-generated insight about product-health gaps (Option B) | P1 |
| HA-5 | As a vendor admin, charts load within 2 seconds of dashboard load | P0 |

## 6.3 Technical Architecture

### 6.3.1 Backend API

#### [NEW] Route in `server/routes/analytics.ts`

```typescript
// GET /api/v1/analytics/health-summary
router.get("/analytics/health-summary", requireAuth, async (req, res) => {
  const vendorId = req.vendorId;

  const [allergens, conditions, diets, totalCustomers] = await Promise.all([
    // Allergen distribution
    db.execute(sql`
      SELECT a.name, COUNT(DISTINCT ca.b2b_customer_id) AS customer_count
      FROM gold.b2b_customer_allergens ca
      JOIN gold.allergens a ON ca.allergen_id = a.id
      JOIN gold.b2b_customers c ON ca.b2b_customer_id = c.id
      WHERE c.vendor_id = ${vendorId}
      GROUP BY a.name ORDER BY customer_count DESC LIMIT 10
    `),
    // Health condition distribution
    db.execute(sql`
      SELECT hc.name, COUNT(DISTINCT chc.b2b_customer_id) AS customer_count
      FROM gold.b2b_customer_health_conditions chc
      JOIN gold.health_conditions hc ON chc.health_condition_id = hc.id
      JOIN gold.b2b_customers c ON chc.b2b_customer_id = c.id
      WHERE c.vendor_id = ${vendorId}
      GROUP BY hc.name ORDER BY customer_count DESC LIMIT 10
    `),
    // Dietary preference distribution
    db.execute(sql`
      SELECT dp.name, COUNT(DISTINCT cdp.b2b_customer_id) AS customer_count
      FROM gold.b2b_customer_dietary_preferences cdp
      JOIN gold.dietary_preferences dp ON cdp.dietary_preference_id = dp.id
      JOIN gold.b2b_customers c ON cdp.b2b_customer_id = c.id
      WHERE c.vendor_id = ${vendorId}
      GROUP BY dp.name ORDER BY customer_count DESC LIMIT 10
    `),
    // Total active customers
    db.execute(sql`
      SELECT COUNT(*) AS total FROM gold.b2b_customers 
      WHERE vendor_id = ${vendorId} AND account_status = 'active'
    `),
  ]);

  res.json({
    allergen_distribution: allergens.rows,
    health_condition_distribution: conditions.rows,
    dietary_preference_distribution: diets.rows,
    total_customers: totalCustomers.rows[0]?.total ?? 0,
  });
});
```

### 6.3.2 Frontend Changes

#### [MODIFY] `app/dashboard/page.tsx`

Insert Health Analytics section between existing stat cards and Quick Actions:

```
Dashboard (Enhanced)
├── Stat Cards (existing 6)
├── 🆕 Health Analytics Section
│   ├── Donut Chart: "Customer Allergen Distribution"
│   ├── Horizontal Bar Chart: "Health Conditions Breakdown"
│   ├── Pill Badges: "Top Dietary Preferences"
│   └── 🧠 AI Insight Card (optional, from RAG): "Actionable recommendation"
├── Quick Actions (existing)
└── Recent Activity (existing)
```

---

## 6.RAG — RAG Team Scope

> **Owner:** RAG Pipeline Engineer
> **Scope:** Optional — only needed for Option B (AI insight generation)

### Deliverables (Option B only)

#### 1. NLG Insight Endpoint (Lightweight)

No new RAG endpoint required. The B2B backend generates the analytics data locally (SQL), then optionally calls an existing LLM endpoint to generate a natural language insight.

If the RAG team prefers a dedicated endpoint:

```json
POST /b2b/analytics-insight
Request: { "allergens": [{"peanut": 23}], "conditions": [...], "total_customers": 100, "total_products": 200 }
Response: { "insight": "32% of your customers have allergen restrictions. Top gap: only 3 products are certified nut-free for your 23 peanut-allergic customers." }
```

---

## 6.4 Acceptance Criteria

- [ ] `GET /api/v1/analytics/health-summary` returns allergen/condition/diet distributions
- [ ] Dashboard renders allergen donut chart, condition bar chart, diet pills
- [ ] Charts render within 2 seconds
- [ ] (Optional) AI insight card shows actionable text when RAG is available

## 6.5 Route Registration

```typescript
import analytics from "./routes/analytics.js";
app.use("/api/v1", analytics);
```

## 6.6 Environment Variables

```env
USE_GRAPH_ANALYTICS=false  # Only used for Option B AI insights
```
