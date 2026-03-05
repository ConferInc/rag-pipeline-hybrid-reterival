# B2B RAG Integration — RAG Team Guide

> **Audience:** RAG Pipeline Engineers
> **Repo:** `rag-pipeline-hybrid-reterival`
> **Purpose:** Everything the RAG team needs to know about the B2B application, integration points, and required deliverables.

---

## 1. B2B Application Overview

### What It Does

NutriB2B is a vendor-management platform for retailers (e.g., Walmart, Wegmans) to manage their product catalogs and customer health data. Vendors ingest:

- **Products** (name, brand, category, nutrition facts, ingredients)
- **Customers** (demographics, allergens, health conditions, dietary preferences, health profiles)

The platform then provides quality scoring, compliance checks, alerts, and search. **This integration adds Graph RAG-powered recommendations, chatbot, safety checks, and search.**

### Key Constraint: Vendor Scoping

> **CRITICAL RULE:** Every B2B query MUST be scoped to a `vendor_id`. A vendor's data is completely isolated. A customer from Vendor A must NEVER appear in results for Vendor B. This applies to ALL RAG endpoints.

**How vendor_id reaches the RAG API:**

1. Vendor admin logs in via Appwrite
2. Express backend extracts `vendor_id` from JWT
3. Express passes `vendor_id` as a parameter to every RAG API call
4. RAG API includes `vendor_id` in every Neo4j MATCH clause

### User Roles

| Role | Description | Views |
|------|-------------|-------|
| Vendor Admin | Primary user — manages products, customers, compliance | All pages |
| Viewer (future) | Read-only access | Dashboard, reports |

---

## 2. B2B Architecture

### System Diagram

```
┌────────────────────────┐
│  nutrib2b-v20          │  Next.js 14 (App Router)
│  Port: 3000            │  Components: shadcn/ui, Tailwind, Lucide
│  Auth: Appwrite SDK    │  State: React Query
└──────────┬─────────────┘
           │ REST API calls
           ▼
┌────────────────────────┐
│  nutriapp-backend      │  Express.js + TypeScript
│  Port: 5000            │  ORM: Drizzle (PostgreSQL)
│  Auth: Appwrite JWT    │  Client: Supabase JS client
│  Features:             │  Middleware: requireAuth, requireRole
│  - Products CRUD       │
│  - Customers CRUD      │
│  - Compliance Engine   │
│  - Quality Scoring     │
│  - Alerts System       │
│  - Ingestion Pipeline  │
└──────────┬─────────────┘
           │ Service-to-service (X-API-Key)
           ▼
┌────────────────────────┐     ┌──────────────┐
│  rag-pipeline (FastAPI)│────▶│  Neo4j 5     │
│  Port: 8000            │     │  Graph DB    │
│  Auth: X-API-Key       │     └──────────────┘
│  NLU + Cypher + LLM    │            ▲
└────────────────────────┘     ┌──────┴──────┐
                               │  OpenAI/LLM │
                               └─────────────┘
```

### Database: PostgreSQL (Supabase — `gold` schema)

**Key B2B Tables:**

| Table | Purpose | Row Count (typical) |
|-------|---------|---------------------|
| `gold.vendors` | Vendor identity | 1-100 |
| `gold.b2b_customers` | Customer identity, vendor-scoped | 100-10K per vendor |
| `gold.b2b_customer_allergens` | Customer → Allergen links with `severity` | 0-5 per customer |
| `gold.b2b_customer_health_conditions` | Customer → Health condition with `severity` | 0-3 per customer |
| `gold.b2b_customer_dietary_preferences` | Customer → Diet preference | 0-3 per customer |
| `gold.b2b_customer_health_profiles` | Calculated metrics: BMR, TDEE, BMI, targets | 1 per customer |
| `gold.products` | Product catalog, vendor-scoped | 50-5K per vendor |
| `gold.product_ingredients` | Product → Ingredient with quantity, unit, order | 2-30 per product |
| `gold.ingredients` | Master ingredient list | 10K+ global |
| `gold.ingredient_allergens` | Ingredient → Allergen with threshold_ppm | Many-to-many |
| `gold.allergens` | Master allergen list with severity_typical | ~15 |
| `gold.health_conditions` | Master health condition list | ~50 |
| `gold.dietary_preferences` | Master diet preference list | ~15 |
| `gold.product_quality_scores` | Quality grade per product | 1 per product |
| `gold.product_categories` | Product category hierarchy | 50-200 per vendor |
| `gold.b2b_compliance_rules` | Compliance rule definitions | 5-20 per vendor |

**Key Columns for RAG:**

- `b2b_customer_allergens.severity`: `mild | moderate | severe | anaphylactic`
- `b2b_customer_health_conditions.severity`: `mild | moderate | severe`
- `product_ingredients.is_primary`: boolean
- `product_ingredients.quantity`, `.unit`: numeric + unit
- `ingredient_allergens.threshold_ppm`: parts per million
- `products.vendor_id`: **ALWAYS used for vendor scoping**

---

## 3. Required RAG API Endpoints

All endpoints use `POST` (except health check). All require `X-API-Key` header.

### 3.1 Health Check

```
GET /health
Response: { "status": "ok", "neo4j": "connected", "pg_sync_last_run": "2024-01-15T10:30:00Z" }
```

### 3.2 All B2B Endpoints

| # | Endpoint | PRD | Purpose | Timeout |
|---|----------|-----|---------|---------|
| 1 | `POST /b2b/recommend-products` | PRD-02 | Recommend products for a customer | 5s |
| 2 | `POST /b2b/search` | PRD-03 | NLP-powered product search | 3s |
| 3 | `POST /b2b/search-suggest` | PRD-03 | "Did You Mean?" suggestions | 2s |
| 4 | `POST /b2b/product-customers` | PRD-04 | Find matching customers for a product | 5s |
| 5 | `POST /b2b/chat` | PRD-05 | B2B domain chatbot | 10s |
| 6 | `POST /b2b/substitutions` | PRD-09 | Smart product substitution | 5s |
| 7 | `POST /b2b/safety-check` | PRD-07 | Cross-reactivity analysis (optional) | 5s |
| 8 | `POST /b2b/product-intel` | PRD-08 | Diet compatibility analysis | 3s |

> **Note:** Endpoints 7 and 8 are optional enhancements. The core safety check and ingredient data are SQL-based. These endpoints add graph-based cross-reactivity and diet compatibility analysis.

### 3.3 Common Request Pattern

Every B2B endpoint receives `vendor_id` as a **required** parameter:

```json
{
  "vendor_id": "uuid",
  ... other parameters ...
}
```

### 3.4 Common Response Pattern

```json
{
  "results": [...],                      // Main data
  "retrieval_time_ms": 320,              // Performance metric
  "explanation": "LLM-generated text",   // Optional NLG explanation
  "intent": "b2b_products_for_diet",     // Detected intent (chat only)
  "structured_data": { ... },            // For chatbot rich content
  "session_id": "uuid"                   // For chatbot sessions
}
```

### 3.5 Error Response Pattern

```json
{
  "error": "vendor_id is required",
  "code": "VALIDATION_ERROR",
  "status": 400
}
```

---

## 4. Service-to-Service Authentication

### X-API-Key Middleware

```python
from fastapi import Request, HTTPException

async def verify_api_key(request: Request):
    api_key = request.headers.get("X-API-Key")
    if api_key != os.environ["RAG_API_KEY"]:
        raise HTTPException(status_code=401, detail="Invalid API key")
```

Register on all `/b2b/*` routes.

---

## 5. B2B NLU — Required Intents

### 5.1 Pattern-Based (Tier 1 — Zero LLM Cost)

Add these regex patterns to `chatbot/nlu.py`:

| Intent | Pattern | Example |
|--------|---------|---------|
| `b2b_products_for_condition` | `(products?)\b.*(for|with).*(customer|client).*(diabet|hypertens|...)` | "Products for diabetic customers" |
| `b2b_products_allergen_free` | `(products?)\b.*(free from|without|no ).*(peanut|gluten|...)` | "Products free from peanuts" |
| `b2b_products_for_diet` | `(products?)\b.*(keto|vegan|vegetarian|gluten.?free|...)` | "Keto-compatible products" |
| `b2b_customers_for_product` | `(customer|client).*(recommend|safe|match).*(product)` | "Which customers can I recommend this to?" |
| `b2b_customers_with_condition` | `(customer|client).*(with|have).*(diabet|allerg|...)` | "List customers with peanut allergy" |
| `b2b_customer_recommendations` | `(recommend|suggest).*(for|to)\b.*[A-Z]` | "Recommendations for John Smith" |
| `b2b_analytics` | `(how many|count|stats?)\b.*(customer|product)` | "How many customers are lactose intolerant?" |
| `b2b_product_compliance` | `(is|check)\b.*(product)\b.*(safe|compliant)` | "Is this product safe for celiacs?" |
| `b2b_product_nutrition` | `(nutrition|nutritional|macros)\b.*(product|item)` | "Nutrition info for Almond Bar" |
| `b2b_generate_report` | `(generate|create)\b.*(report|summary|matrix)` | "Generate allergen safety report" |

### 5.2 LLM-Based (Tier 2 — For ambiguous queries)

For queries not matching Tier 1 patterns, pass to LLM with system prompt:

```
Classify this B2B query into one of the intents: [list all intents].
Extract entities: allergens, health_conditions, dietary_preferences, product_names, customer_names, nutrient_thresholds.
```

---

## 6. Required Cypher Query Builders

Add to `cypher_query_generator.py`:

| Function | PRD | Purpose |
|----------|-----|---------|
| `_build_b2b_recommend_products()` | PRD-02 | Customer-specific product ranking |
| `_build_b2b_search_products()` | PRD-03 | NLP-powered product search |
| `_build_b2b_search_suggest()` | PRD-03 | Entity extraction for suggestions |
| `_build_b2b_product_customers()` | PRD-04 | Product → matching customers |
| `_build_b2b_products_for_condition()` | PRD-05 | Products for health conditions |
| `_build_b2b_products_allergen_free()` | PRD-05 | Allergen-free product search |
| `_build_b2b_products_for_diet()` | PRD-05 | Diet-compatible products |
| `_build_b2b_customers_with_condition()` | PRD-05 | Find customers by health attribute |
| `_build_b2b_customer_recommendations()` | PRD-05 | Named customer recommendations |
| `_build_b2b_analytics()` | PRD-05 | Count/stats queries |
| `_build_b2b_substitution_category()` | PRD-09 | Category+nutrition substitution |
| `_build_b2b_substitution_health_aware()` | PRD-09 | Health-safe substitution |
| `_build_b2b_substitution_ingredient()` | PRD-09 | Ingredient Jaccard similarity |
| `_build_b2b_report_allergen_safety()` | PRD-10 | Allergen safety report |
| `_build_b2b_report_diet_coverage()` | PRD-10 | Diet coverage report |
| `_build_b2b_report_safety_matrix()` | PRD-10 | Product × allergen matrix |

> **CRITICAL:** Every Cypher query MUST include `vendor_id` in MATCH clause:
>
> ```cypher
> MATCH (p:Product)-[:SOLD_BY]->(v:Vendor {id: $vendor_id})
> ```

---

## 7. PG → Neo4j B2B Sync

### 7.1 Sync Script: `sync/b2b_pg_sync.py`

**Requirements:**

- Connect to PG via `PG_READ_URL` (read-only credentials)
- Use MERGE-based Cypher (idempotent, safe to re-run)
- Sync relationship **properties** (severity, quantity, etc.) — not just edges
- Output sync stats (rows synced per table, duration)
- Cron-compatible: exit 0 on success, non-zero on failure

### 7.2 Tables to Sync

| Priority | PG Table | Neo4j Target | Frequency | Properties to Sync |
|----------|---------|-------------|-----------|-------------------|
| P0 | `b2b_customers` | `(:B2BCustomer)` | 15 min | id, full_name, email, age, gender, vendor_id |
| P0 | `b2b_customer_allergens` | `[:ALLERGIC_TO]` | 15 min | **severity** (critical!) |
| P0 | `b2b_customer_health_conditions` | `[:HAS_CONDITION]` | 15 min | **severity** |
| P0 | `b2b_customer_dietary_preferences` | `[:FOLLOWS_DIET]` | 15 min | strictness |
| P0 | `b2b_customer_health_profiles` | `(:B2BHealthProfile)` | 15 min | bmr, tdee, bmi, targets |
| P0 | `products` | `(:Product)` | 15 min | All nutrition cols |
| P0 | `product_ingredients` | `[:CONTAINS_INGREDIENT]` | 15 min | quantity, unit, is_primary, order |
| P1 | `ingredient_allergens` | `(Ingredient)-[:CONTAINS_ALLERGEN]->(Allergen)` | 6 hrs | threshold_ppm |
| P1 | `product_dietary_preferences` | `[:COMPATIBLE_WITH_DIET]` | 6 hrs | — |
| P2 | `health_condition_ingredient_restrictions` | `[:RESTRICTS_INGREDIENT]` | 6 hrs | restriction_type |

### 7.3 Example MERGE Script

```python
MERGE_B2B_CUSTOMER = """
MERGE (c:B2BCustomer {id: $id})
SET c.full_name = $full_name,
    c.email = $email,
    c.age = $age,
    c.gender = $gender,
    c.vendor_id = $vendor_id,
    c.updated_at = datetime()
WITH c
MATCH (v:Vendor {id: $vendor_id})
MERGE (c)-[:BELONGS_TO_VENDOR]->(v)
"""

MERGE_ALLERGIC_TO = """
MATCH (c:B2BCustomer {id: $customer_id})
MATCH (a:Allergen {id: $allergen_id})
MERGE (c)-[r:ALLERGIC_TO]->(a)
SET r.severity = $severity
"""
```

---

## 8. B2B Chatbot System Prompt

```
You are NutriB2B Assistant, a domain-specific AI for the NutriB2B platform.
You help vendor administrators manage their product catalog and customer health data.

You can ONLY answer questions about:
- Products in the vendor's catalog
- Customers and their health profiles
- Product recommendations based on health data
- Allergen safety and dietary compliance
- Nutritional analysis of products
- Customer analytics and reports

You CANNOT answer questions about:
- Topics outside nutrition and food
- Personal medical advice
- Competitor products
- Pricing or business strategy
- General knowledge or conversational topics

When answering:
- Scope all data to the current vendor
- Format results as structured tables
- Include count summaries
- Cite specific products/customers in your responses
```

---

## 9. Environment Variables (RAG API)

```env
# Neo4j connection
NEO4J_URI=bolt://neo4j:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=<secure-password>
NEO4J_DATABASE=neo4j

# PostgreSQL read-only (for sync)
PG_READ_URL=postgresql://<read-only-user>:<password>@<supabase-host>:5432/postgres

# Service-to-service auth
RAG_API_KEY=<shared-secret-matching-express>

# LLM
OPENAI_API_KEY=<key>
LLM_MODEL=gpt-4o-mini
```

---

## 10. Testing & Validation

### What the B2B Team Expects

1. **Health endpoint** (`GET /health`) must return `{ status: "ok" }` when Neo4j is connected
2. **All endpoints** must return valid JSON matching the response schemas above
3. **Timeout**: Endpoints must respond within their timeout (see Section 3.2)
4. **Error handling**: Invalid `vendor_id` → 400, invalid `X-API-Key` → 401, Neo4j unavailable → 503
5. **Vendor isolation**: Verify that passing `vendor_id=A` never returns data scoped to vendor B
6. **Empty results**: When no data matches, return `{ results: [], ... }` — NOT an error

### Test Queries for Validation

```
1. "Products for diabetic customers" → Should return products
2. "Customers allergic to peanuts" → Should return customers
3. "Is Almond Bar safe for celiacs?" → Should return compliance check
4. "What's the weather today?" → Should return off-topic response
5. Search for "high protein keto" → Should return scored products
6. Substitute for product X → Should return similar products
7. Cross-vendor test: vendor_id=A should not return vendor_id=B data
```

---

## 11. Recommended Build Order

```
Phase 1 (Foundation):
  ├── FastAPI wrapper with /health endpoint
  ├── X-API-Key middleware
  ├── PG→Neo4j sync script (P0 tables)
  └── Neo4j schema expansion (constraints, indexes)

Phase 2 (Core Features):
  ├── POST /b2b/recommend-products
  ├── POST /b2b/search + /b2b/search-suggest
  └── POST /b2b/product-customers

Phase 3 (Chatbot):
  ├── B2B NLU patterns (Tier 1 regex)
  ├── B2B Cypher builders (all 16)
  └── POST /b2b/chat

Phase 4 (Enhancements):
  ├── POST /b2b/substitutions
  ├── POST /b2b/safety-check
  ├── POST /b2b/product-intel
  └── Report generation support
```
