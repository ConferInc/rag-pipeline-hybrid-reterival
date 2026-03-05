# PRD-10: Report Generation via Chatbot

> **Tech Stack:** Next.js (frontend), Express + Drizzle ORM (backend), FastAPI (RAG API), Neo4j 5 (graph DB), PostgreSQL `gold` schema
> **Auth:** Appwrite (B2B auth) → Express validates JWT → Express calls RAG API with `X-API-Key`
> **Repos:** `nutrib2b-v20` (frontend), `nutriapp-backend` (backend), `rag-pipeline-hybrid-reterival` (RAG API)
> **Depends On:** PRD-01 (Foundation & RAG Infra), **PRD-05 (B2B Chatbot)** — must be built first

---

## 10.1 Overview

Extend the B2B chatbot (PRD-05) with a **report generation** capability: vendor admins can ask for reports in natural language, and the chatbot generates structured data + summary + CSV export link.

**Why this matters:** Currently vendors must manually navigate to compliance/products/customers pages, apply filters, and mentally correlate data. Natural language report generation lets them say "Generate a report of all customers allergic to peanuts and the products they should avoid" and get an instant, comprehensive answer.

**Current State:**

- B2B Chatbot (PRD-05) exists with NLU patterns +Cypher generators
- No report-specific intent
- No CSV export from chat

## 10.2 User Stories

| ID | Story | Priority |
|----|-------|----------|
| RG-1 | As a vendor admin, I ask the chatbot to generate a safety report and get structured results | P0 |
| RG-2 | As a vendor admin, the report includes a summary paragraph | P0 |
| RG-3 | As a vendor admin, I can download the full report as CSV | P1 |
| RG-4 | As a vendor admin, I can ask for diet coverage reports ("How many products are keto-compatible?") | P1 |
| RG-5 | As a vendor admin, I can ask for customer segment reports ("Generate recommendations for diabetic customers") | P2 |

## 10.3 Technical Architecture

### 10.3.1 New Intent: `b2b_generate_report`

| Sub-type | Example Query |
|----------|---------------|
| `allergen_safety` | "Generate a report of customers allergic to peanuts and products they should avoid" |
| `diet_coverage` | "Report on how many products are compatible with each diet" |
| `customer_recommendations` | "Generate recommendations for all customers with diabetes" |
| `product_safety_matrix` | "Create a safety matrix of all products vs all customer allergens" |

### 10.3.2 Report Flow

```
Vendor Admin: "Generate a report of customers allergic to peanuts 
              and products they should avoid"

1. NLU → intent: b2b_generate_report
         entities: { allergen: "peanut", report_type: "allergen_safety" }

2. Cypher → Cross-reference query:
   MATCH (c:B2BCustomer)-[:ALLERGIC_TO]->(a:Allergen {code: 'peanut'})
   MATCH (c)-[:BELONGS_TO_VENDOR]->(v:Vendor {id: $vendor_id})
   MATCH (p:Product)-[:SOLD_BY]->(v)-[:CONTAINS_INGREDIENT]->(i)-[:CONTAINS_ALLERGEN]->(a)
   RETURN c.full_name, c.email, p.name, p.brand

3. Format → Structured table + CSV-ready data

4. LLM → Summarize: "Found 23 customers with peanut allergies and 8 products 
   containing peanuts. 184 total conflict pairs. Top product: PB Crackers 
   (conflicts with 20 customers)."

5. Response → Table + Summary + CSV download button
```

### 10.3.3 Backend API

#### [MODIFY] `server/routes/chat.ts`

The chat endpoint (PRD-05) already handles responses. For reports, add CSV generation:

```typescript
// POST /api/v1/chat/export — export last report as CSV
router.post("/chat/export", requireAuth, async (req, res) => {
  const { session_id, report_id } = req.body;
  // Retrieve report data from session or regenerate
  // Convert to CSV and stream
  res.setHeader("Content-Type", "text/csv");
  res.setHeader("Content-Disposition", `attachment; filename="report-${report_id}.csv"`);
  // Stream CSV rows
});
```

### 10.3.4 Frontend Changes

#### [MODIFY] `components/chatbot/ChatMessage.tsx`

Add report-specific rendering inside the chat:

```
Chat Message (Report Type)
├── 📊 Summary paragraph (LLM-generated)
├── Table preview (first 10 rows, scrollable)
│   ├── Customer | Product | Allergen | Severity
│   ├── Jane Doe | PB Crackers | Peanut | Anaphylactic
│   └── ...
├── "Show all X rows" expand button
└── 📥 "Download Full Report (CSV)" button
```

---

## 10.RAG — RAG Team Scope

> **Owner:** RAG Pipeline Engineer

### Deliverables

#### 1. New Intent: `b2b_generate_report`

Add to `chatbot/nlu.py`:

```python
"b2b_generate_report": 
    r"(generate|create|produce|build|give me)\b.*(report|summary|matrix|analysis|overview)\b"
```

#### 2. New Cypher Query Builders

Add to `cypher_query_generator.py`:

```python
def _build_b2b_report_allergen_safety(entities: dict) -> tuple[str, dict]:
    """Report: customers with specific allergens + unsafe products."""
    
def _build_b2b_report_diet_coverage(entities: dict) -> tuple[str, dict]:
    """Report: diet preference coverage (how many products per diet)."""
    
def _build_b2b_report_customer_recommendations(entities: dict) -> tuple[str, dict]:
    """Report: batch recommendations for a customer segment."""

def _build_b2b_report_safety_matrix(entities: dict) -> tuple[str, dict]:
    """Report: full product × allergen safety matrix."""
```

All must include `vendor_id` scoping.

#### 3. Response Format for Reports

When intent is `b2b_generate_report`, the response MUST include `structured_data` with type `report`:

```json
{
  "response": "LLM-generated summary...",
  "intent": "b2b_generate_report",
  "structured_data": {
    "type": "report",
    "report_type": "allergen_safety",
    "columns": ["Customer", "Product", "Allergen", "Severity"],
    "rows": [
      ["Jane Doe", "PB Crackers", "Peanut", "Anaphylactic"],
      ...
    ],
    "total_rows": 184,
    "summary": { "customers_affected": 23, "products_flagged": 8 }
  }
}
```

---

## 10.4 Acceptance Criteria

- [ ] Chat query "Generate allergen safety report" returns structured table + summary
- [ ] Report includes all product-customer allergen conflicts
- [ ] CSV export downloads complete report data
- [ ] Diet coverage report shows product count per diet
- [ ] Reports are vendor-scoped
- [ ] Depends on PRD-05 chatbot being functional

## 10.5 Route Registration

```typescript
// Extends chat.ts from PRD-05
```

## 10.6 Environment Variables

```env
# Uses same flag as chatbot:
USE_GRAPH_CHATBOT=false
```
