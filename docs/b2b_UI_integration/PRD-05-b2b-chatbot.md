# PRD-05: B2B Domain Chatbot

> **Tech Stack:** Next.js (frontend), Express + Drizzle ORM (backend), FastAPI (RAG API), Neo4j 5 (graph DB), PostgreSQL `gold` schema
> **Auth:** Appwrite (B2B auth) → Express validates JWT → Express calls RAG API with `X-API-Key`
> **Repos:** `nutrib2b-v20` (frontend), `nutriapp-backend` (backend), `rag-pipeline-hybrid-reterival` (RAG API)
> **Depends On:** PRD-01 (Foundation & RAG Infra)

---

## 5.1 Overview

Build a **domain-specific chatbot** for the B2B app that answers vendor admin queries based on app data only. It connects to the existing RAG pipeline's chatbot stack (NLU → Action Orchestrator → Response Generator) but with B2B-specific intents, vendor-scoped queries, and a professional system prompt.

**Why this matters:** Vendor admins currently have no NL interface to query their data. They must navigate to specific pages, apply filters, and manually interpret results. A chatbot enables queries like: "Which customers are allergic to peanuts?" or "Show me products safe for diabetic customers" — instant data retrieval in natural language.

**Current State:**

- RAG Pipeline: Full chatbot stack exists for B2C: `nlu.py` (2-tier regex → LLM), `action_orchestrator.py`, `response_generator.py`, `session.py`, + 15 Cypher query builders. Architecture is proven.
- Backend: No `/chat` proxy route. No `b2b_chat_sessions` table (created in PRD-01).
- Frontend: No chatbot widget in B2B app.

**SQL Fallback:** If RAG is down, chatbot shows "Service temporarily unavailable — please try again later."

## 5.2 User Stories

| ID | Story | Priority |
|----|-------|----------|
| CB-1 | As a vendor admin, I open a chat widget from any page in the app | P0 |
| CB-2 | As a vendor admin, I ask "Show products for diabetic customers" and get relevant products | P0 |
| CB-3 | As a vendor admin, the chatbot only answers questions about my vendor's data | P0 |
| CB-4 | As a vendor admin, I see quick-action suggestion chips on first open | P1 |
| CB-5 | As a vendor admin, my chat history persists within a session (30 min TTL) | P1 |
| CB-6 | As a vendor admin, product/customer results display as structured cards inside the chat | P1 |
| CB-7 | As a vendor admin, the chatbot refuses off-topic questions (weather, news, etc.) | P0 |

## 5.3 Technical Architecture

### 5.3.1 Backend API

#### [NEW] `server/routes/chat.ts`

```typescript
import { Router } from "express";
import { requireAuth } from "../middleware/auth.js";
import { ragChat } from "../services/ragClient.js";

const router = Router();

// POST /api/v1/chat
router.post("/chat", requireAuth, async (req, res) => {
  const { message, session_id } = req.body;
  const vendorId = req.vendorId;
  const userId = req.userId;

  if (!message?.trim()) {
    return res.status(400).json({ error: "Message is required" });
  }

  const ragResult = await ragChat({
    message,
    vendor_id: vendorId,
    user_id: userId,
    session_id: session_id || null,
  });

  if (!ragResult) {
    return res.json({
      response: "The chat service is temporarily unavailable. Please try again in a moment.",
      intent: null,
      session_id: session_id,
      fallback: true,
    });
  }

  res.json(ragResult);
});

export default router;
```

### 5.3.2 B2B-Specific Intents

| Intent | Type | Example Queries |
|--------|------|-----------------|
| `b2b_products_for_condition` | Read | "Products for customers with diabetes" |
| `b2b_products_allergen_free` | Read | "Show products free from peanuts and dairy" |
| `b2b_products_for_diet` | Read | "List all keto-compatible products" |
| `b2b_customers_for_product` | Read | "Which customers can I recommend Chobani yogurt to?" |
| `b2b_customers_with_condition` | Read | "List customers with hypertension" |
| `b2b_customer_recommendations` | Read | "What products should I recommend to John Smith?" |
| `b2b_product_nutrition` | Read | "What's the nutrition profile of product X?" |
| `b2b_product_compliance` | Read | "Is this product safe for customers with celiac?" |
| `b2b_analytics` | Read | "How many customers are lactose intolerant?" |
| `b2b_generate_report` | Read | "Generate a report of customers allergic to peanuts" (see PRD-10) |

### 5.3.3 NLP Pipeline Flow

```
User Input: "Give me product list for customers who are allergic to peanuts"

Stage 1: NLU (nlu.py) — Tier 1 Regex
  → Pattern match: "product" + "allergic"
  → Intent: b2b_products_allergen_free
  → Fall to LLM for detailed entity extraction

Stage 2: Entity Extraction (extractor_classifier.py)
  → { intent: "b2b_products_allergen_free",
      entities: { allergens: ["peanut"], query_type: "products_for_customers" } }

Stage 3: Cypher Generation (cypher_query_generator.py)
  → MATCH (p:Product)-[:SOLD_BY]->(v:Vendor {id: $vendor_id})
    WHERE NOT EXISTS { MATCH (p)-[:CONTAINS_INGREDIENT]->(i)-[:CONTAINS_ALLERGEN]->(a:Allergen)
                       WHERE a.code = 'peanut' }
    RETURN p.id, p.name, p.brand LIMIT 20

Stage 4: Response Generation (response_generator.py)
  → "I found 12 peanut-free products in your catalog:
     1. Almond Protein Bar (NutriCo) — 280 cal, 32g protein
     2. Oat Milk Crackers (HealthyCo) — 150 cal, 4g protein
     ..."
```

### 5.3.4 System Prompt for B2B Chatbot

```
You are NutriB2B Assistant, a domain-specific AI for the NutriB2B platform.
You help vendor administrators manage their product catalog and customer health data.

You can ONLY answer questions about:
- Products in the vendor's catalog
- Customers and their health profiles
- Product recommendations based on health data
- Allergen safety and dietary compliance
- Nutritional analysis of products
- Customer health analytics and reports

You CANNOT answer questions about:
- Topics outside nutrition and food
- Personal medical advice
- Competitor products not in the catalog
- Pricing strategy or business decisions
- General knowledge or conversational topics

When answering, always scope your response to the current vendor's data.
Format product/customer results as structured lists with key metrics.
```

### 5.3.5 Frontend Changes

#### [NEW] `components/chatbot/B2BChatbot.tsx`

Floating chat widget, accessible from every page:

```
┌────────────────────────────────────────────────────────────────┐
│  💬 NutriB2B Assistant                                   ─ ✕  │
│━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━│
│                                                                │
│  🤖 Hello! I can help you with product recommendations,       │
│     customer matching, and nutritional analysis.               │
│                                                                │
│  Try asking:                                                   │
│  ┌───────────────────────┐  ┌──────────────────────────┐      │
│  │ 🔍 Products for       │  │ 👥 Customers with        │      │
│  │    diabetics          │  │    nut allergies         │      │
│  └───────────────────────┘  └──────────────────────────┘      │
│  ┌───────────────────────┐  ┌──────────────────────────┐      │
│  │ 🥜 Peanut-free        │  │ 📊 Customer health       │      │
│  │    products           │  │    analytics             │      │
│  └───────────────────────┘  └──────────────────────────┘      │
│                                                                │
│━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━│
│  Type a message...                                       Send  │
└────────────────────────────────────────────────────────────────┘
```

#### Component Structure

```
components/chatbot/
├── B2BChatbot.tsx          — Main container (floating widget)
├── ChatMessage.tsx         — Individual message bubble
├── ChatInput.tsx           — Text input with send button
├── ChatProductCard.tsx     — Product result in chat
├── ChatCustomerCard.tsx    — Customer result in chat
└── ChatSuggestions.tsx     — Quick-action suggestion chips
```

#### [MODIFY] `app/layout.tsx` or `components/AppShell.tsx`

Add chatbot widget to the global layout:

```tsx
<AppShell>
  {children}
  <B2BChatbot />  {/* Floating, bottom-right */}
</AppShell>
```

---

## 5.RAG — RAG Team Scope

> **Repo:** `rag-pipeline-hybrid-reterival`
> **Owner:** RAG Pipeline Engineer

### Deliverables

#### 1. `POST /b2b/chat` Endpoint

**Request:**

```json
{
  "message": "Show me products free from peanuts and dairy",
  "vendor_id": "uuid",
  "user_id": "uuid",
  "session_id": "uuid-or-null"
}
```

**Response:**

```json
{
  "response": "I found 12 products in your catalog that are free from peanuts and dairy:\n\n1. **Almond Protein Bar** (NutriCo) — 280 cal, 32g protein\n2. **Oat Milk Crackers** (HealthyCo) — 150 cal, 4g protein\n...",
  "intent": "b2b_products_allergen_free",
  "entities": {
    "allergens": ["peanut", "dairy"]
  },
  "session_id": "session-uuid",
  "structured_data": {
    "type": "product_list",
    "items": [
      { "id": "uuid", "name": "Almond Protein Bar", "brand": "NutriCo", "calories": 280, "protein_g": 32 }
    ]
  }
}
```

#### 2. B2B NLU Patterns (Tier 1 — Zero LLM Cost)

Add to `chatbot/nlu.py`:

```python
B2B_RULE_PATTERNS: dict[str, str] = {
    "b2b_products_for_condition": 
        r"(products?|items?|goods?)\b.*(for|with|having)\b.*(customer|client).*(diabet|hypertens|cholesterol|celiac|kidney|heart)",
    "b2b_products_allergen_free": 
        r"(products?|items?)\b.*(free from|without|allergen.?free|no )\b.*(peanut|dairy|gluten|soy|egg|wheat|shellfish|tree.?nut|milk|lactose)",
    "b2b_products_for_diet": 
        r"(products?|items?|list)\b.*(keto|vegan|vegetarian|gluten.?free|paleo|low.?carb|low.?fat|high.?protein)",
    "b2b_customers_for_product": 
        r"(which|what|find|show)\b.*(customer|client).*(recommend|suitable|safe|match).*(product|item)",
    "b2b_customers_with_condition": 
        r"(list|show|find|how many)\b.*(customer|client).*(with|have|having)\b.*(diabet|allerg|hypertens|intoleran|celiac)",
    "b2b_customer_recommendations": 
        r"(recommend|suggest|what product).*(for|to)\b.*[A-Z][a-z]+",
    "b2b_analytics": 
        r"(how many|count|percentage|stats?|analytics?)\b.*(customer|client|product)",
    "b2b_product_compliance": 
        r"(is|are|check|verify)\b.*(product|item)\b.*(safe|compliant|suitable|ok)\b.*(for|with)",
}
```

#### 3. B2B Cypher Query Builders

Add to `cypher_query_generator.py`:

- `_build_b2b_products_for_condition(entities)` — Products safe for customers with specific health conditions
- `_build_b2b_products_allergen_free(entities)` — Allergen-free product search
- `_build_b2b_products_for_diet(entities)` — Diet-compatible product search
- `_build_b2b_customers_for_product(entities)` — Find matching customers for a product
- `_build_b2b_customers_with_condition(entities)` — List customers by health attribute
- `_build_b2b_customer_recommendations(entities)` — Recommendations for named customer
- `_build_b2b_analytics(entities)` — Count/stats queries
- `_build_b2b_product_compliance(entities)` — Check product safety for conditions

All queries MUST include `vendor_id` scoping.

#### 4. Session Management

- Create or retrieve sessions by `session_id`
- Store last 5 messages in session context for multi-turn conversation
- Auto-expire sessions after 30 minutes of inactivity
- Persist to `b2b_chat_sessions` table (created in PRD-01) or in-memory with PG backup

#### 5. Domain Guardrails

Implement B2B system prompt (Section 5.3.4) and off-topic response:

```python
OFF_TOPIC_RESPONSE = {
    "response": "I can only help with questions about your product catalog, "
                "customer health profiles, and nutritional analysis. "
                "Could you try rephrasing your question?",
    "intent": "off_topic",
}
```

---

## 5.4 Acceptance Criteria

- [ ] `POST /api/v1/chat` returns structured chatbot responses
- [ ] Query "Products free from peanuts" returns allergen-free products
- [ ] Query "Which customers have diabetes?" returns customer list
- [ ] Off-topic queries get domain guardrail response
- [ ] Chat sessions persist within 30-min TTL
- [ ] Chatbot widget renders on every page
- [ ] Quick-action chips trigger example queries
- [ ] Results display as structured cards (products/customers)
- [ ] All queries are vendor-scoped

## 5.5 Route Registration

```typescript
import chat from "./routes/chat.js";
app.use("/api/v1", chat);
```

## 5.6 Environment Variables

```env
USE_GRAPH_CHATBOT=false  # Set to 'true' to enable chatbot
```
