# 06-rag-pipeline-changes

# RAG Pipeline — Required Changes (Handoff Document)

**For:** RAG Pipeline Engineer**Repo:** `rag-pipeline-hybrid-reterival`**Context:** We are integrating the RAG pipeline with the B2C nutrition app (Express backend + Next.js frontend). The B2C app currently uses raw SQL for everything. Your changes will let the app call the RAG pipeline as an HTTP API to serve smarter search, meal planning, grocery lists, and a chatbot.

***

## Table of Contents

1. [Your Current Codebase (What You Have)](#1-your-current-codebase)
2. [Why These Changes Are Needed](#2-why-these-changes)
3. [Change 1: FastAPI Wrapper (api.py)](#3-change-1-fastapi-wrapper)
4. [Change 2: Dockerfile](#4-change-2-dockerfile)
5. [Change 3: Extended NLU (extractor\_classifier.py)](#5-change-3-extended-nlu)
6. [Change 4: Chatbot Engine (New Module)](#6-change-4-chatbot-engine)
7. [Change 5: Orchestrator Modifications](#7-change-5-orchestrator-modifications)
8. [Change 6: PG→Neo4j Sync Scripts](#8-change-6-sync-scripts)
9. [Change 7: Neo4j Schema Expansion](#9-change-7-neo4j-schema)
10. [Change 8: GraphSAGE Automation](#10-change-8-graphsage)
11. [Environment Variables](#11-environment-variables)
12. [Testing Checklist](#12-testing-checklist)

***

## 1. Your Current Codebase

Here's your repo's file map and what each file does:

```
rag-pipeline-hybrid-reterival/
├── extractor_classifier.py        ← NLU: 8 intents, LLM-based intent+entity extraction
├── cypher_query_generator.py      ← Generates parameterized Cypher from intent+entities
├── embedding_config.yaml          ← Vector index specs, label-text rules, intent mappings
├── requirements.txt               ← 4 deps: neo4j, PyYAML, openai, python-dotenv
│
├── rag_pipeline/
│   ├── __init__.py
│   ├── cli.py                     ← CLI with 5 commands: semantic-search, structural-search, etc.
│   ├── config.py                  ← EmbeddingConfig dataclass loaded from YAML
│   ├── neo4j_client.py            ← Neo4j driver factory (env-based config)
│   │
│   ├── orchestrator/
│   │   ├── orchestrator.py        ← Main pipeline: NLU → semantic → structural → cypher
│   │   └── cypher_runner.py       ← Wraps cypher_query_generator + driver.session.run()
│   │
│   ├── retrieval/
│   │   ├── semantic.py            ← Vector similarity search using Neo4j native indexes
│   │   ├── structural.py          ← GraphSAGE-based similarity + k-hop expansion
│   │   ├── service.py             ← SemanticRetrievalRequest model + retrieve_semantic()
│   │   └── types.py               ← Result type definitions
│   │
│   ├── embeddings/
│   │   ├── base.py                ← QueryEmbedder abstract class
│   │   └── openai_embedder.py     ← OpenAI embedding implementation
│   │
│   ├── augmentation/
│   │   ├── condense.py            ← Deduplication + ranking for LLM context
│   │   └── prompt_builder.py      ← Builds [SYSTEM] + [CONTEXT] + [USER QUERY] prompt
│   │
│   └── generation/
│       └── generator.py           ← LLM response generation via OpenAI API
│
└── docs/                          ← Documentation files
```

**Key points about the current state:**

* The pipeline is **CLI-only** — no web server, no HTTP API
* It has **8 intents** (find\_recipe, compare\_foods, check\_diet, etc.)
* The orchestrator calls 3 retrievals in sequence: semantic → structural → cypher
* Structural retrieval only runs for `find_recipe` and `find_recipe_by_pantry` intents
* All LLM calls go to OpenAI-compatible API via `openai` library (supports LiteLLM proxy)
* Neo4j graph is **partially populated** — some data exists but gaps need filling

***

## 2. Why These Changes Are Needed

The B2C app (Express/Node.js backend + Next.js frontend) needs to call your pipeline over HTTP. Here's what each B2C feature needs from you:

| B2C Feature        | What the Express Backend Sends You                   | What You Return                                   |
| ------------------ | ---------------------------------------------------- | ------------------------------------------------- |
| **Search**         | User's NL query + filters + userId                   | Ranked recipe IDs + scores + reasons              |
| **Meal Plan**      | Member profiles + dietary constraints + meal history | Scored recipe candidates                          |
| **Grocery List**   | Ingredient IDs + user allergens + budget             | Product recommendations + substitutions           |
| **Dashboard Feed** | User ID + preferences                                | Personalized recipe recommendations + reasons     |
| **Scanner**        | Product ID + user allergens                          | Allergen-safe alternative products                |
| **Chatbot**        | User message + session history + userId              | Intent + response + optional action               |
| **Meal Patterns**  | User ID + date range                                 | Variety score, nutrition gaps, frequency analysis |

**The Express backend will call your endpoints with a shared API key (****`X-API-Key`****&#x20;header). You do NOT need to validate user JWTs — Express handles that.**

***

## 3. Change 1: FastAPI Wrapper

### Why

The pipeline currently only works as a CLI (`cli.py`). The B2C backend needs to call it over HTTP. FastAPI wraps your existing modules as a REST API.

### \[NEW] `api.py`

```python
"""
FastAPI server wrapping the RAG pipeline for B2C app integration.

Architecture:
  Express Backend --HTTP/REST--> This API --> Neo4j + LLM
  (Handles user auth)            (Handles retrieval + generation)
"""
from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAI
from pydantic import BaseModel, Field

from rag_pipeline.config import load_embedding_config
from rag_pipeline.embeddings.openai_embedder import OpenAIQueryEmbedder
from rag_pipeline.neo4j_client import create_neo4j_driver, neo4j_settings_from_env
from rag_pipeline.orchestrator.orchestrator import orchestrate, OrchestratorResult


# ── Startup / Shutdown ─────────────────────────────────────────────────────

load_dotenv()

# Shared resources (initialized once at startup, reused for all requests)
_driver = None
_cfg = None
_embedder = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize Neo4j driver + embedder on startup, close on shutdown."""
    global _driver, _cfg, _embedder
    
    neo_settings = neo4j_settings_from_env()
    _driver = create_neo4j_driver(neo_settings)
    _cfg = load_embedding_config(os.getenv("EMBEDDING_CONFIG", "embedding_config.yaml"))
    _embedder = OpenAIQueryEmbedder(
        client=OpenAI(
            base_url=os.getenv("OPENAI_BASE_URL"),
            api_key=os.getenv("OPENAI_API_KEY"),
        ),
        model=os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"),
    )
    
    yield  # App runs here
    
    _driver.close()


app = FastAPI(title="NutriB2C RAG API", version="1.0.0", lifespan=lifespan)

# Only allow traffic from Express backend (internal Coolify network)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("ALLOWED_ORIGINS", "http://express-backend:5000").split(","),
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)


# ── Auth ───────────────────────────────────────────────────────────────────

async def verify_api_key(x_api_key: str = Header(..., alias="X-API-Key")):
    """
    Service-to-service authentication.
    
    WHY: This API is not user-facing. The Express backend validates user JWTs
    (Appwrite) and then calls us with this shared API key. This prevents
    unauthorized services from accessing the RAG pipeline.
    """
    expected = os.getenv("RAG_API_KEY")
    if not expected or x_api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ── Request/Response Schemas ───────────────────────────────────────────────

class SearchRequest(BaseModel):
    """Natural language search query from the B2C search page."""
    query: str = Field(..., max_length=500, description="User's search text")
    customer_id: str | None = Field(None, description="B2C customer UUID (for personalization)")
    filters: dict[str, Any] = Field(default_factory=dict, description="Structured filters (diets, allergens, etc.)")
    limit: int = Field(20, ge=1, le=50)

class FeedRequest(BaseModel):
    """Dashboard feed recommendation request."""
    customer_id: str = Field(..., description="B2C customer UUID")
    preferences: dict[str, Any] = Field(default_factory=dict)
    limit: int = Field(20, ge=1, le=50)

class MealCandidateRequest(BaseModel):
    """Meal plan candidate scoring request."""
    customer_id: str
    members: list[dict[str, Any]] = Field(..., description="Household member profiles with diets/allergens")
    meal_history: list[str] = Field(default_factory=list, description="Recipe IDs eaten in last 30 days")
    date_range: dict[str, str] = Field(..., description="{'start': 'YYYY-MM-DD', 'end': 'YYYY-MM-DD'}")
    meals_per_day: list[str] = Field(default_factory=lambda: ["breakfast", "lunch", "dinner"])
    limit: int = Field(50, ge=1, le=100)

class ProductRequest(BaseModel):
    """Product recommendation for grocery list."""
    ingredient_ids: list[str]
    customer_allergens: list[str] = Field(default_factory=list)
    budget_max: float | None = None

class AlternativesRequest(BaseModel):
    """Scanner: find allergen-safe product alternatives."""
    product_id: str
    customer_allergens: list[str]
    budget_preference: str = "any"  # "cheapest" | "closest" | "any"

class RecommendationResult(BaseModel):
    """Single recommendation with explainability."""
    id: str
    score: float
    reasons: list[str] = Field(default_factory=list, description="WHY this was recommended")
    metadata: dict[str, Any] = Field(default_factory=dict)

class SearchResponse(BaseModel):
    results: list[RecommendationResult]
    intent: str
    entities: dict[str, Any]
    retrieval_time_ms: float


# ── Endpoints ──────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Health check — Coolify uses this to know the service is alive."""
    try:
        _driver.verify_connectivity()
        return {"status": "ok", "neo4j": "connected"}
    except Exception as e:
        return {"status": "degraded", "neo4j": str(e)}


@app.post("/search/hybrid", response_model=SearchResponse, dependencies=[Depends(verify_api_key)])
async def search_hybrid(req: SearchRequest):
    """
    Natural language search with hybrid retrieval.
    
    CALLED BY: Express GET /api/v1/recipes?q=...
    
    HOW IT WORKS:
    1. extract_intent(query) → intent + entities
    2. orchestrate() → semantic + structural + cypher results
    3. Merge + score + deduplicate
    4. Return ranked recipe IDs with reasons
    
    The Express backend then hydrates these IDs with full recipe data from PostgreSQL
    (images, full nutrition, etc.) since Neo4j doesn't store everything.
    """
    start = time.time()
    
    result = orchestrate(
        _driver,
        cfg=_cfg,
        embedder=_embedder,
        user_query=req.query,
        customer_node_id=req.customer_id,
        top_k=req.limit,
        database=os.getenv("NEO4J_DATABASE"),
    )
    
    recommendations = _merge_results(result, limit=req.limit)
    
    return SearchResponse(
        results=recommendations,
        intent=result.intent,
        entities=result.entities,
        retrieval_time_ms=(time.time() - start) * 1000,
    )


@app.post("/recommend/feed", response_model=SearchResponse, dependencies=[Depends(verify_api_key)])
async def recommend_feed(req: FeedRequest):
    """
    Personalized dashboard feed.
    
    CALLED BY: Express GET /api/v1/feed
    
    Unlike search, this doesn't start from a text query. Instead it:
    1. Loads the customer's Neo4j profile (allergens, diets, conditions)
    2. Runs Cypher to find compliant recipes
    3. Runs semantic search for variety
    4. Runs structural for collaborative filtering (if enough interaction data)
    5. Merges with explainable reasons
    """
    # Build internal query from customer profile
    internal_query = f"Recommend personalized recipes for customer {req.customer_id}"
    
    start = time.time()
    result = orchestrate(
        _driver, cfg=_cfg, embedder=_embedder,
        user_query=internal_query,
        customer_node_id=req.customer_id,
        top_k=req.limit,
        database=os.getenv("NEO4J_DATABASE"),
    )
    
    return SearchResponse(
        results=_merge_results(result, limit=req.limit),
        intent="recommend_feed",
        entities={},
        retrieval_time_ms=(time.time() - start) * 1000,
    )


@app.post("/recommend/meal-candidates", dependencies=[Depends(verify_api_key)])
async def recommend_meal_candidates(req: MealCandidateRequest):
    """
    Pre-scored recipe candidates for meal planning.
    
    CALLED BY: Express POST /api/v1/meal-plans/generate
    
    WHY THIS MATTERS: Currently the Express backend fetches ALL recipes from SQL
    and sends the entire catalog to the LLM for meal plan generation. This is slow
    and the LLM makes poor choices because it has no nutrition/preference context.
    
    With this endpoint, we pre-score candidates using the graph:
    - Filter out allergen-violating recipes
    - Boost recipes that fill nutritional gaps
    - Penalize recently eaten recipes (variety)
    - Score by GraphSAGE similarity (collaborative filtering)
    
    The Express backend then sends only the top 50 candidates to the LLM.
    """
    # TODO: Implement custom Cypher for meal plan scoring
    # For now, reuse orchestrator with meal-plan-specific query
    start = time.time()
    
    diets = []
    allergens = []
    for member in req.members:
        diets.extend(member.get("diets", []))
        allergens.extend(member.get("allergens", []))
    
    query = f"Find {req.limit} recipes for meal planning: diets={diets}, avoid allergens={allergens}"
    
    result = orchestrate(
        _driver, cfg=_cfg, embedder=_embedder,
        user_query=query,
        customer_node_id=req.customer_id,
        top_k=req.limit,
        database=os.getenv("NEO4J_DATABASE"),
    )
    
    return {
        "candidates": _merge_results(result, limit=req.limit),
        "retrieval_time_ms": (time.time() - start) * 1000,
    }


@app.post("/recommend/products", dependencies=[Depends(verify_api_key)])
async def recommend_products(req: ProductRequest):
    """
    Product recommendations for grocery list generation.
    
    CALLED BY: Express POST /api/v1/grocery-lists/generate
    
    For each ingredient the meal plan needs, find the best matching product that:
    - Doesn't contain the customer's allergens
    - Is within budget (if specified)
    - Has substitution options
    """
    # Direct Cypher queries against Neo4j
    products = []
    with _driver.session(database=os.getenv("NEO4J_DATABASE")) as session:
        for ingredient_id in req.ingredient_ids:
            rows = session.run("""
                MATCH (i:Ingredient {id: $ingredientId})<-[:CONTAINS_INGREDIENT]-(p:Product)
                WHERE p.status = 'active'
                AND NOT EXISTS {
                    MATCH (p)-[:CONTAINS_ALLERGEN]->(a:Allergen)
                    WHERE a.code IN $allergens
                }
                RETURN p.id AS id, p.name AS name, p.brand AS brand, p.price AS price
                ORDER BY p.price ASC
                LIMIT 3
            """, ingredientId=ingredient_id, allergens=req.customer_allergens)
            
            for row in rows:
                products.append({
                    "ingredient_id": ingredient_id,
                    "product_id": row["id"],
                    "name": row["name"],
                    "brand": row["brand"],
                    "price": row["price"],
                })
    
    return {"products": products}


@app.post("/recommend/alternatives", dependencies=[Depends(verify_api_key)])
async def recommend_alternatives(req: AlternativesRequest):
    """
    Find allergen-safe alternatives for a scanned product.
    
    CALLED BY: Express GET /api/v1/scan/:barcode (as a follow-up enrichment)
    
    WHY: When a user scans a product and gets allergen warnings, the app should
    immediately suggest safe alternatives. This query traverses [:CAN_SUBSTITUTE]
    relationships in the graph.
    """
    with _driver.session(database=os.getenv("NEO4J_DATABASE")) as session:
        rows = session.run("""
            MATCH (p:Product {id: $productId})-[:CAN_SUBSTITUTE]->(alt:Product)
            WHERE alt.status = 'active'
            AND NOT EXISTS {
                MATCH (alt)-[:CONTAINS_ALLERGEN]->(a:Allergen)
                WHERE a.code IN $allergens
            }
            RETURN alt.id AS id, alt.name AS name, alt.brand AS brand,
                   alt.price AS price, p.price - alt.price AS savings
            ORDER BY savings DESC
            LIMIT 5
        """, productId=req.product_id, allergens=req.customer_allergens)
        
        return {"alternatives": [dict(row) for row in rows]}


@app.post("/analytics/meal-patterns", dependencies=[Depends(verify_api_key)])
async def meal_patterns(customer_id: str, days: int = 14):
    """
    Analyze eating patterns from meal log data in the graph.
    
    CALLED BY: Express GET /api/v1/meal-log/patterns
    
    WHY: The Express backend stores meal logs in PostgreSQL, but pattern analysis
    (variety across cuisines, ingredient diversity, nutritional gap detection)
    is much more natural as graph traversals than SQL JOINs.
    """
    with _driver.session(database=os.getenv("NEO4J_DATABASE")) as session:
        # Variety score
        variety = session.run("""
            MATCH (c:B2CCustomer {id: $customerId})-[:LOGGED_MEAL]->(ml:MealLog)
            WHERE ml.log_date >= date() - duration({days: $days})
            MATCH (ml)-[:CONTAINS_ITEM]->(mli:MealLogItem)-[:OF_RECIPE]->(r:Recipe)
            OPTIONAL MATCH (r)-[:BELONGS_TO_CUISINE]->(cu:Cuisine)
            OPTIONAL MATCH (r)-[:USES_INGREDIENT]->(i:Ingredient)
            RETURN COUNT(DISTINCT cu) AS uniqueCuisines,
                   COUNT(DISTINCT i) AS uniqueIngredients,
                   COUNT(DISTINCT r) AS uniqueRecipes,
                   COUNT(mli) AS totalMeals
        """, customerId=customer_id, days=days)
        
        variety_data = dict(variety.single())
        
        # Most repeated recipes
        repeats = session.run("""
            MATCH (c:B2CCustomer {id: $customerId})-[:LOGGED_MEAL]->(ml:MealLog)
            WHERE ml.log_date >= date() - duration({days: $days})
            MATCH (ml)-[:CONTAINS_ITEM]->(mli:MealLogItem)-[:OF_RECIPE]->(r:Recipe)
            RETURN r.id AS recipeId, r.title AS title, COUNT(mli) AS frequency
            ORDER BY frequency DESC
            LIMIT 5
        """, customerId=customer_id, days=days)
        
        return {
            "variety": variety_data,
            "most_repeated": [dict(r) for r in repeats],
            "days_analyzed": days,
        }


# ── Helpers ────────────────────────────────────────────────────────────────

def _merge_results(orch: OrchestratorResult, *, limit: int = 20) -> list[RecommendationResult]:
    """
    Merge semantic + structural + cypher results into a single ranked list.
    
    Score fusion: finalScore = 0.5*cypher + 0.3*semantic + 0.2*structural
    """
    seen: dict[str, RecommendationResult] = {}
    
    # Cypher results (highest weight — most precise)
    for i, row in enumerate(orch.cypher_results[:limit]):
        rid = str(row.get("id", row.get("r.id", f"cypher_{i}")))
        if rid not in seen:
            seen[rid] = RecommendationResult(
                id=rid, score=0.0, reasons=["Matches your query criteria"], metadata=row
            )
        seen[rid].score += 0.5 * (1.0 - i / max(len(orch.cypher_results), 1))
    
    # Semantic results
    for i, item in enumerate(orch.semantic_results[:limit]):
        rid = str(getattr(item, "node_id", f"sem_{i}"))
        if rid not in seen:
            seen[rid] = RecommendationResult(
                id=rid, score=0.0, reasons=["Semantically similar to your query"], metadata={}
            )
        seen[rid].score += 0.3 * (1.0 - i / max(len(orch.semantic_results), 1))
    
    # Sort by score descending
    ranked = sorted(seen.values(), key=lambda r: r.score, reverse=True)
    return ranked[:limit]
```

> \[!NOTE]
> The `_merge_results` helper above is a basic implementation. You should refine the score fusion weights and reason generation based on testing with real data.

### \[MODIFY] `requirements.txt`

```diff
 neo4j
 PyYAML
 openai
 python-dotenv
+fastapi
+uvicorn[standard]
+pydantic>=2.0
+sentence-transformers
+psycopg2-binary
```

**Why each new dependency:**

* `fastapi` — HTTP framework (lightweight, async, auto-generates OpenAPI docs)
* `uvicorn` — ASGI server to run FastAPI
* `pydantic` — Request/response validation (already a FastAPI dependency, but pinned >=2.0)
* `sentence-transformers` — For embedding generation (if not using OpenAI embeddings)
* `psycopg2-binary` — For PG→Neo4j sync scripts (connecting to Supabase PostgreSQL)

***

## 4. Change 2: Dockerfile

### Why

Your pipeline needs to run as a Docker container on Coolify. Currently there's no Dockerfile.

### \[NEW] `Dockerfile`

```docker
# Multi-stage build for smaller image size
FROM python:3.11-slim AS builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM python:3.11-slim
# Security: Non-root user
RUN useradd -r -s /bin/false appuser
WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY . .

# Own files as appuser
RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Health check for Coolify
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

# Run with 2 workers (adjust based on server resources)
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
```

***

## 5. Change 3: Extended NLU

### Why

The chatbot needs 9 additional intents that don't exist yet. The B2C app lets users create meal plans, generate grocery lists, log meals, and update preferences — all via natural language in a chat interface.

### \[MODIFY] `extractor_classifier.py`

**Current state:** 8 intents in `SYSTEM_PROMPT`, 8 in `sanity_check.valid_intents`.

**Changes needed:**

1. **Add 9 new intents** to `SYSTEM_PROMPT`:

```python
# Add after "rank_results" in SYSTEM_PROMPT:
- "create_meal_plan"               User wants to generate a meal plan for a period.
- "modify_meal_plan"               User wants to change an existing meal plan item.
- "show_meal_plan"                 User wants to see their current meal plan.
- "create_grocery_list"            User wants to generate a shopping list.
- "modify_grocery_list"            User wants to add/remove items from a list.
- "log_meal"                       User logging what they ate.
- "update_preferences"             User updating diet/allergen preferences.
- "show_nutrition_summary"         User asking about their nutrition intake/trends.
- "clarify"                        User providing clarification to a previous bot question.
```

1. **Add new entities** to `SYSTEM_PROMPT`:

```python
# Add to ENTITIES section:
- "plan_duration"        str          "this week", "next 3 days", "Monday to Friday"
- "plan_start_date"      str          ISO date or relative ("tomorrow", "next Monday")
- "meals_per_day"        list[str]    ["breakfast", "lunch", "dinner"]
- "budget_amount"        float        Budget constraint in USD
- "grocery_items"        list[str]    Items to add/remove from list
- "grocery_action"       str          "add" | "remove"
- "meal_description"     str          "I had oatmeal with berries for breakfast"
- "meal_type"            str          "breakfast" | "lunch" | "dinner" | "snack"
- "new_diet"             list[str]    New dietary preferences to set
- "new_allergens"        list[str]    Newly declared allergens
```

1. **Add examples** to `SYSTEM_PROMPT`:

```python
# Add examples:
User: "Plan my meals for this week, I'm vegetarian"
{"intent":"create_meal_plan","entities":{"diet":["Vegetarian"],"plan_duration":"this week","meals_per_day":["breakfast","lunch","dinner"]}}

User: "Replace Wednesday dinner with something gluten-free"
{"intent":"modify_meal_plan","entities":{"plan_duration":"Wednesday","meal_type":"dinner","diet":["Gluten-Free"]}}

User: "Generate my grocery list for this week's meal plan"
{"intent":"create_grocery_list","entities":{}}

User: "I had oatmeal with berries for breakfast"
{"intent":"log_meal","entities":{"meal_description":"oatmeal with berries","meal_type":"breakfast"}}

User: "How's my protein intake this week?"
{"intent":"show_nutrition_summary","entities":{"nutrient":"Protein","plan_duration":"this week"}}
```

1. **Update&#x20;****`valid_intents`****&#x20;in&#x20;****`sanity_check()`****:**

```python
valid_intents = {
    "find_recipe", "find_recipe_by_pantry", "get_nutritional_info",
    "compare_foods", "check_diet_compliance", "check_substitution",
    "get_substitution_suggestion", "rank_results",
    # NEW:
    "create_meal_plan", "modify_meal_plan", "show_meal_plan",
    "create_grocery_list", "modify_grocery_list",
    "log_meal", "update_preferences", "show_nutrition_summary", "clarify",
}
```

> \[!IMPORTANT]
> **Do NOT break existing intents.** The Cypher query generator already handles the original 8 intents. The new intents will be handled by the chatbot action orchestrator (see Change 4), not by `cypher_query_generator.py`.

***

## 6. Change 4: Chatbot Engine

### Why

The B2C app will have a chat widget on every page. Users can type natural language to search, plan meals, generate grocery lists, and log meals. The chatbot engine processes messages, maintains conversation context, and decides what action to take.

### \[NEW] `chatbot/` directory

```
chatbot/
├── __init__.py
├── nlu.py              ← Hybrid NLU (rule-based first, LLM fallback)
├── session.py           ← Conversation session management
├── action_orchestrator.py ← Routes intents to actions
└── response_generator.py  ← Domain-grounded LLM response generation
```

#### `chatbot/nlu.py` — Hybrid NLU

```python
"""
Two-tier NLU to minimize LLM costs:
- Tier 1: Regex/keyword patterns — instant, zero cost
- Tier 2: LLM extraction — 200-500ms, costs tokens

WHY HYBRID: 
At scale, calling the LLM for every "hi" or "show my meal plan" wastes money.
Simple intents can be matched with regex. Only ambiguous/complex queries 
need the LLM. This approach handles ~60% of messages without any LLM call.
"""
import re
from dataclasses import dataclass
from typing import Any

from extractor_classifier import extract_intent, sanity_check


@dataclass
class NLUResult:
    intent: str
    entities: dict[str, Any]
    source: str  # "rules" or "llm" — for debugging and cost tracking


RULE_PATTERNS: dict[str, str] = {
    "greeting":              r"^(hi|hello|hey|good morning|good evening|sup|yo)\b",
    "out_of_domain":         r"\b(weather|news|stock|joke|code|program|politics)\b",
    "find_recipe":           r"(find|show|search|give me|suggest|recommend)\b.*(recipe|meal|dish|food)",
    "find_recipe_by_pantry": r"(what can i|cook with|make with|i have)\b.*(fridge|pantry|ingredients?)",
    "create_meal_plan":      r"(plan|create|generate|make|draw)\b.*(meal|week|menu|eating|diet)",
    "show_meal_plan":        r"(show|view|see|what'?s)\b.*(meal plan|my plan|this week)",
    "create_grocery_list":   r"(grocery|shopping|buy|shop)\b.*(list|items)",
    "log_meal":              r"(i (had|ate|eaten|just)|log|record)\b.*(breakfast|lunch|dinner|snack|meal)",
    "show_nutrition_summary":r"(how am i|my nutrition|my intake|protein|calories)\b.*(doing|week|today|summary)",
    "check_diet_compliance": r"(is|can i|allowed|safe)\b.*(diet|keto|vegan|gluten)",
    "compare_foods":         r"(compare|vs|versus|difference|better)\b",
}


async def extract_hybrid(message: str, context: dict | None = None) -> NLUResult:
    """
    Try rule-based extraction first. If no match or entities can't be extracted,
    fall back to LLM-based extraction.
    """
    normalized = message.strip().lower()
    
    # Tier 1: Rule-based matching
    for intent, pattern in RULE_PATTERNS.items():
        if re.search(pattern, normalized, re.IGNORECASE):
            entities = _extract_entities_by_rules(normalized, intent)
            if entities is not None:
                return NLUResult(intent=intent, entities=entities, source="rules")
    
    # Tier 2: LLM extraction (for complex/ambiguous queries)
    try:
        import json
        raw = extract_intent(message)
        parsed = json.loads(raw)
        check = sanity_check(parsed)
        if check is True:
            return NLUResult(
                intent=parsed["intent"],
                entities=parsed["entities"],
                source="llm",
            )
    except Exception:
        pass
    
    # If everything fails, treat as a general recipe search
    return NLUResult(intent="find_recipe", entities={"dish": message}, source="fallback")


def _extract_entities_by_rules(message: str, intent: str) -> dict[str, Any] | None:
    """
    Extract entities using simple keyword matching.
    Returns None if entities can't be reliably extracted (signals LLM fallback).
    """
    if intent in ("greeting", "out_of_domain"):
        return {}
    
    if intent == "show_meal_plan":
        return {}  # No entities needed
    
    if intent == "show_nutrition_summary":
        return {}  # No entities needed
    
    if intent == "create_grocery_list":
        return {}  # No entities needed
    
    # For intents that need richer entity extraction, return None → LLM fallback
    return None
```

#### `chatbot/session.py` — Session Management

```python
"""
Chat session management.

WHY: Multi-turn conversation needs memory. When a user says "swap Tuesday dinner",
the bot needs to know which meal plan they're referring to (from a previous message).

Storage: In-memory dict for MVP, move to Redis when scaling.
Session expires after 30 min of inactivity.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
import uuid


@dataclass
class ChatMessage:
    role: str  # "user" | "assistant" | "system"
    content: str
    intent: str | None = None
    entities: dict[str, Any] | None = None
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class PendingAction:
    action_id: str
    intent: str
    entities: dict[str, Any]
    preview: dict[str, Any]  # What to show the user for confirmation


@dataclass
class ChatSession:
    session_id: str
    customer_id: str
    history: list[ChatMessage] = field(default_factory=list)
    pending_action: PendingAction | None = None
    current_meal_plan_id: str | None = None
    current_grocery_list_id: str | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    last_activity: datetime = field(default_factory=datetime.utcnow)
    
    @property
    def is_expired(self) -> bool:
        return datetime.utcnow() - self.last_activity > timedelta(minutes=30)
    
    def add_message(self, role: str, content: str, **kwargs):
        self.history.append(ChatMessage(role=role, content=content, **kwargs))
        # Keep last 20 messages (sliding window to control LLM context size)
        if len(self.history) > 20:
            self.history = self.history[-20:]
        self.last_activity = datetime.utcnow()


# In-memory session store (MVP). Replace with Redis for production.
_sessions: dict[str, ChatSession] = {}


def get_or_create_session(customer_id: str, session_id: str | None = None) -> ChatSession:
    if session_id and session_id in _sessions:
        session = _sessions[session_id]
        if not session.is_expired and session.customer_id == customer_id:
            return session
    
    # Create new session
    new_id = session_id or str(uuid.uuid4())
    session = ChatSession(session_id=new_id, customer_id=customer_id)
    _sessions[new_id] = session
    return session


def cleanup_expired():
    """Remove expired sessions. Call periodically."""
    expired = [sid for sid, s in _sessions.items() if s.is_expired]
    for sid in expired:
        del _sessions[sid]
```

#### `chatbot/action_orchestrator.py`

```python
"""
Routes chatbot intents to concrete actions.

WHY: Read-only intents (find_recipe, compare_foods) execute immediately.
Write intents (create_meal_plan, log_meal) need user confirmation first.
This prevents the bot from accidentally creating meal plans.

HOW CONFIRMATION WORKS:
1. User: "Plan my meals for the week"
2. Bot returns action_preview with a summary + action_id
3. Frontend shows preview with [Save] [Regenerate] buttons
4. User clicks Save → Express calls POST /chat/action with action_id + confirmed=true
5. Express executes the write action server-side (mealPlan.generateMealPlan())
6. Bot confirms: "Your meal plan is saved!"

SECURITY: action_ids are signed with HMAC to prevent tampering.
"""
from enum import Enum


class ActionType(Enum):
    READ_ONLY = "read"      # Execute immediately, no confirmation
    WRITE = "write"         # Requires user confirmation before execution


ACTION_REGISTRY: dict[str, ActionType] = {
    # Existing (read-only — execute immediately via orchestrator)
    "find_recipe":                  ActionType.READ_ONLY,
    "find_recipe_by_pantry":        ActionType.READ_ONLY,
    "get_nutritional_info":         ActionType.READ_ONLY,
    "compare_foods":                ActionType.READ_ONLY,
    "check_diet_compliance":        ActionType.READ_ONLY,
    "check_substitution":           ActionType.READ_ONLY,
    "get_substitution_suggestion":  ActionType.READ_ONLY,
    "rank_results":                 ActionType.READ_ONLY,
    "show_meal_plan":               ActionType.READ_ONLY,
    "show_nutrition_summary":       ActionType.READ_ONLY,
    
    # New (write — require confirmation)
    "create_meal_plan":             ActionType.WRITE,
    "modify_meal_plan":             ActionType.WRITE,
    "create_grocery_list":          ActionType.WRITE,
    "modify_grocery_list":          ActionType.WRITE,
    "log_meal":                     ActionType.WRITE,
    "update_preferences":           ActionType.WRITE,
    
    # Conversational (no action needed)
    "greeting":                     ActionType.READ_ONLY,
    "out_of_domain":                ActionType.READ_ONLY,
    "clarify":                      ActionType.READ_ONLY,
}
```

#### `chatbot/response_generator.py`

```python
"""
Domain-grounded response generation for the chatbot.

WHY DOMAIN-GROUNDED: The LLM should ONLY respond using data from Neo4j retrieval.
It should NEVER hallucinate recipe names, nutrition facts, or product info.

The response generator takes:
1. The retrieved context (from orchestrator)
2. The conversation history
3. The user's profile (allergens, diets)
And produces a natural language response that references ONLY the retrieved data.
"""
from rag_pipeline.generation.generator import generate_response
from rag_pipeline.augmentation.prompt_builder import build_augmented_prompt


CHATBOT_SYSTEM_PROMPT = """You are NutriBot, an AI nutrition assistant for the NutriB2C app.

STRICT RULES:
1. ONLY answer questions about nutrition, recipes, meal planning, grocery shopping,
   dietary preferences, allergens, and health conditions.
2. NEVER provide medical advice. For health questions, recommend consulting
   a healthcare provider.
3. ALL recipe/product/nutrition data comes from the retrieval context provided.
   Do NOT hallucinate recipes, ingredients, or nutritional values.
4. When showing recipes, always include: title, cook time, calories, protein,
   and any relevant allergen warnings.
5. When creating meal plans, respect ALL user constraints.
6. If you're unsure, say so honestly rather than guessing.
7. Keep responses concise and actionable.
8. For out-of-domain queries, politely redirect.

CONTEXT:
{retrieval_context}

CONVERSATION HISTORY:
{conversation_history}
"""


def generate_chat_response(
    retrieval_context: str,
    conversation_history: str,
    user_message: str,
) -> str:
    prompt = CHATBOT_SYSTEM_PROMPT.format(
        retrieval_context=retrieval_context,
        conversation_history=conversation_history,
    )
    full_prompt = f"[SYSTEM]\n{prompt}\n\n[USER QUERY]\n{user_message}"
    return generate_response(full_prompt, temperature=0.3)
```

### Chat Processing Endpoint in `api.py`

Add this endpoint to `api.py`:

```python
@app.post("/chat/process", dependencies=[Depends(verify_api_key)])
async def chat_process(
    message: str,
    customer_id: str,
    session_id: str | None = None,
):
    """
    Process a chatbot message.
    
    CALLED BY: Express POST /api/v1/chat/message
    
    Flow:
    1. Get/create session
    2. Extract intent (hybrid NLU)
    3. If read-only → run retrieval → generate response
    4. If write action → return action preview for confirmation
    5. Add to conversation history
    """
    from chatbot.nlu import extract_hybrid
    from chatbot.session import get_or_create_session
    from chatbot.action_orchestrator import ACTION_REGISTRY, ActionType
    from chatbot.response_generator import generate_chat_response
    
    session = get_or_create_session(customer_id, session_id)
    session.add_message("user", message)
    
    # Step 1: Extract intent
    nlu_result = await extract_hybrid(message, context={"history": session.history})
    
    action_type = ACTION_REGISTRY.get(nlu_result.intent, ActionType.READ_ONLY)
    
    # Step 2: Handle based on action type
    if action_type == ActionType.READ_ONLY:
        # Run retrieval + generate response
        orch_result = orchestrate(
            _driver, cfg=_cfg, embedder=_embedder,
            user_query=message,
            customer_node_id=customer_id,
            top_k=5,
            database=os.getenv("NEO4J_DATABASE"),
        )
        
        from rag_pipeline.augmentation.prompt_builder import build_augmented_prompt
        context = build_augmented_prompt(orch_result, message)
        
        history_text = "\n".join(
            f"{m.role}: {m.content}" for m in session.history[-6:]
        )
        
        response = generate_chat_response(context, history_text, message)
        session.add_message("assistant", response, intent=nlu_result.intent)
        
        return {
            "response": response,
            "intent": nlu_result.intent,
            "entities": nlu_result.entities,
            "nlu_source": nlu_result.source,
            "session_id": session.session_id,
            "action_required": False,
        }
    
    else:
        # Write action → return preview for confirmation
        import hashlib, hmac
        action_id = hmac.new(
            os.getenv("RAG_API_KEY", "").encode(),
            f"{session.session_id}:{nlu_result.intent}".encode(),
            hashlib.sha256,
        ).hexdigest()[:16]
        
        from chatbot.session import PendingAction
        session.pending_action = PendingAction(
            action_id=action_id,
            intent=nlu_result.intent,
            entities=nlu_result.entities,
            preview={"intent": nlu_result.intent, "entities": nlu_result.entities},
        )
        
        return {
            "response": f"I'll {nlu_result.intent.replace('_', ' ')} for you. Please confirm.",
            "intent": nlu_result.intent,
            "entities": nlu_result.entities,
            "nlu_source": nlu_result.source,
            "session_id": session.session_id,
            "action_required": True,
            "action_id": action_id,
            "action_preview": session.pending_action.preview,
        }
```

***

## 7. Change 5: Orchestrator Modifications

### \[MODIFY] `rag_pipeline/orchestrator/orchestrator.py`

**Change 1:** Add new intents to `STRUCTURAL_INTENTS`:

```python
# Current:
STRUCTURAL_INTENTS = {"find_recipe", "find_recipe_by_pantry"}

# Change to:
STRUCTURAL_INTENTS = {
    "find_recipe", "find_recipe_by_pantry",
    "create_meal_plan",  # Collaborative filtering helps meal planning
    "show_nutrition_summary",  # Compare with similar users
}
```

**Why:** Meal planning benefits from collaborative filtering ("users with similar profiles ate these recipes"). Nutrition summary can compare against similar users' patterns.

**Change 2:** Add progressive weight adjustment for collaborative filtering cold-start:

```python
# Add to orchestrate() before Step 3:
def _get_interaction_count(driver, customer_id, database=None):
    """Check how many interactions this user has for cold-start handling."""
    with driver.session(database=database) as session:
        result = session.run("""
            MATCH (c:B2CCustomer {id: $id})-[r:RATED|SAVED|VIEWED]->()
            RETURN COUNT(r) AS count
        """, id=customer_id)
        return result.single()["count"]
```

***

## 8. Change 6: PG→Neo4j Sync Scripts

### Why

The B2C app stores all data in PostgreSQL (Supabase). Neo4j needs this data for graph queries. The sync script reads from PG and writes to Neo4j using idempotent `MERGE` statements.

### \[NEW] `sync/pg_sync.py`

This is a large script — the key parts are:

```python
"""
PostgreSQL → Neo4j data synchronization.

Reads from Supabase PostgreSQL (read-only connection).
Writes to Neo4j using MERGE (idempotent — safe to re-run).

Run frequency:
- P0 tables (customer profiles): Every 15 minutes
- P1 tables (recipes, products): Every 6 hours
- P2 tables (interactions, logs): Every 15 minutes

Security:
- PG connection uses READ-ONLY credentials
- Neo4j credentials from environment variables only
"""
import os
import psycopg2
from neo4j import GraphDatabase

def sync_customers(pg_conn, neo4j_driver):
    """Sync b2c_customers → B2CCustomer nodes."""
    # ... SELECT from PG, MERGE into Neo4j

def sync_customer_allergens(pg_conn, neo4j_driver):
    """Sync b2c_customer_allergens → [:ALLERGIC_TO] relationships."""
    # ...

def sync_recipes(pg_conn, neo4j_driver):
    """Sync recipes → Recipe nodes."""
    # ...

# ... (one function per table, see implementation plan for full MERGE scripts)
```

***

## 9. Change 7: Neo4j Schema Expansion

### Why

The current Neo4j graph only has a handful of node types (Recipe, Ingredient, Allergen, Diet, Cuisine, B2CCustomer, Product). For the new features (meal planning, meal log analysis, grocery list, scanner alternatives, collaborative filtering), the graph needs **14 additional node types** and **22 new relationship types**.

These are created by the PG→Neo4j sync script using `MERGE` (idempotent — safe to re-run). But **before running the sync for the first time**, you must create the uniqueness constraints and indexes listed below, otherwise `MERGE` will be slow and may create duplicates.

### Step 1: Run Uniqueness Constraints (REQUIRED before first sync)

These prevent duplicate nodes when the sync script runs `MERGE`. Without these, if the sync runs twice, you'll get duplicate nodes.

```cypher
// ── Uniqueness Constraints ────────────────────────────────────
// Run each of these ONCE in Neo4j Browser or via the driver

CREATE CONSTRAINT mealplan_id IF NOT EXISTS FOR (n:MealPlan) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT mealplanitem_id IF NOT EXISTS FOR (n:MealPlanItem) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT meallog_id IF NOT EXISTS FOR (n:MealLog) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT meallogitem_id IF NOT EXISTS FOR (n:MealLogItem) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT shoppinglist_id IF NOT EXISTS FOR (n:ShoppingList) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT shoppinglistitem_id IF NOT EXISTS FOR (n:ShoppingListItem) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT reciperating_id IF NOT EXISTS FOR (n:RecipeRating) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT scanevent_id IF NOT EXISTS FOR (n:ScanEvent) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT household_id IF NOT EXISTS FOR (n:Household) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT householdmember_id IF NOT EXISTS FOR (n:HouseholdMember) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT householdbudget_id IF NOT EXISTS FOR (n:HouseholdBudget) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT healthprofile_id IF NOT EXISTS FOR (n:HealthProfile) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT meallogtemplate_id IF NOT EXISTS FOR (n:MealLogTemplate) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT meallogstreak_id IF NOT EXISTS FOR (n:MealLogStreak) REQUIRE n.id IS UNIQUE;
```

### Step 2: Create Performance Indexes

These indexes speed up the Cypher queries used by the API endpoints (search, meal patterns, feed, etc.):

```cypher
// MealLog.log_date — used by meal pattern analysis (POST /analytics/meal-patterns)
// Queries filter by "ml.log_date >= date() - duration({days: 14})"
CREATE INDEX meallog_date IF NOT EXISTS FOR (n:MealLog) ON (n.log_date);

// MealLog.b2c_customer_id — used to find a customer's meal logs
CREATE INDEX meallog_customer IF NOT EXISTS FOR (n:MealLog) ON (n.b2c_customer_id);

// MealPlan.b2c_customer_id — used by chatbot "show my meal plan" intent
CREATE INDEX mealplan_customer IF NOT EXISTS FOR (n:MealPlan) ON (n.b2c_customer_id);

// MealPlan date range — used to find current/upcoming meal plans
CREATE INDEX mealplan_dates IF NOT EXISTS FOR (n:MealPlan) ON (n.start_date, n.end_date);

// ScanEvent.b2c_customer_id — used by scanner history queries
CREATE INDEX scanevent_customer IF NOT EXISTS FOR (n:ScanEvent) ON (n.b2c_customer_id);

// ShoppingList.b2c_customer_id — used by grocery list recommendations
CREATE INDEX shoppinglist_customer IF NOT EXISTS FOR (n:ShoppingList) ON (n.b2c_customer_id);

// RecipeRating.recipe_id — used by collaborative filtering to find popular recipes
CREATE INDEX reciperating_recipe IF NOT EXISTS FOR (n:RecipeRating) ON (n.recipe_id);
```

### Step 3: 14 New Node Types (Created by Sync Script)

These are the `MERGE` statements the sync script (`sync/pg_sync.py`) will run. Each reads rows from PostgreSQL and creates/updates the corresponding Neo4j node.

```cypher
// ── 1. MealPlan ───────────────────────────────────────────────
// SOURCE: gold.meal_plans table
// USED BY: POST /recommend/meal-candidates, chatbot "show my meal plan"
MERGE (mp:MealPlan {id: $id})
SET mp.b2c_customer_id = $customerId,
    mp.household_id = $householdId,
    mp.name = $name,
    mp.start_date = date($startDate),
    mp.end_date = date($endDate),
    mp.status = $status,
    mp.created_at = datetime($createdAt)

// ── 2. MealPlanItem ───────────────────────────────────────────
// SOURCE: gold.meal_plan_items table
// USED BY: Meal plan swap analysis, variety scoring
MERGE (mpi:MealPlanItem {id: $id})
SET mpi.meal_plan_id = $mealPlanId,
    mpi.recipe_id = $recipeId,
    mpi.day_index = $dayIndex,
    mpi.meal_type = $mealType,
    mpi.servings = $servings

// ── 3. MealLog ────────────────────────────────────────────────
// SOURCE: gold.meal_logs table
// USED BY: POST /analytics/meal-patterns (variety score, repeat detection)
MERGE (ml:MealLog {id: $id})
SET ml.b2c_customer_id = $customerId,
    ml.log_date = date($logDate),
    ml.total_calories = $totalCalories,
    ml.total_protein_g = $totalProtein,
    ml.total_carbs_g = $totalCarbs,
    ml.total_fat_g = $totalFat,
    ml.water_ml = $waterMl

// ── 4. MealLogItem ────────────────────────────────────────────
// SOURCE: gold.meal_log_items table
// USED BY: Pattern analysis — links what was eaten to which recipe/product
MERGE (mli:MealLogItem {id: $id})
SET mli.meal_log_id = $mealLogId,
    mli.meal_type = $mealType,
    mli.recipe_id = $recipeId,
    mli.product_id = $productId,
    mli.custom_name = $customName,
    mli.servings = $servings,
    mli.calories = $calories,
    mli.protein_g = $proteinG

// ── 5. ShoppingList ───────────────────────────────────────────
// SOURCE: gold.shopping_lists table
// USED BY: Graph traversal from MealPlan → ShoppingList → Products
MERGE (sl:ShoppingList {id: $id})
SET sl.b2c_customer_id = $customerId,
    sl.meal_plan_id = $mealPlanId,
    sl.name = $name,
    sl.status = $status,
    sl.total_estimated_cost = $totalCost

// ── 6. ShoppingListItem ──────────────────────────────────────
// SOURCE: gold.shopping_list_items table
// USED BY: Product substitution queries (POST /recommend/products)
MERGE (sli:ShoppingListItem {id: $id})
SET sli.shopping_list_id = $shoppingListId,
    sli.product_id = $productId,
    sli.ingredient_id = $ingredientId,
    sli.quantity = $quantity,
    sli.unit = $unit,
    sli.checked = $checked

// ── 7. RecipeRating ───────────────────────────────────────────
// SOURCE: gold.recipe_ratings table
// USED BY: Collaborative filtering — "users who liked X also liked Y"
MERGE (rr:RecipeRating {id: $id})
SET rr.b2c_customer_id = $customerId,
    rr.recipe_id = $recipeId,
    rr.rating = $rating,
    rr.review_text = $reviewText,
    rr.created_at = datetime($createdAt)

// ── 8. ScanEvent ──────────────────────────────────────────────
// SOURCE: gold.scan_history table
// USED BY: POST /recommend/alternatives, scanner purchase pattern analysis
MERGE (se:ScanEvent {id: $id})
SET se.b2c_customer_id = $customerId,
    se.product_id = $productId,
    se.barcode = $barcode,
    se.scan_source = $scanSource,
    se.scanned_at = datetime($scannedAt)

// ── 9. Household ──────────────────────────────────────────────
// SOURCE: gold.households table
// USED BY: Meal planning for entire household (multiple dietary constraints)
MERGE (h:Household {id: $id})
SET h.name = $name,
    h.owner_b2c_customer_id = $ownerId

// ── 10. HouseholdMember ───────────────────────────────────────
// SOURCE: gold.household_members table
// USED BY: Multi-member meal plans (each member has different allergens/diets)
MERGE (hm:HouseholdMember {id: $id})
SET hm.household_id = $householdId,
    hm.name = $name,
    hm.age = $age,
    hm.role = $role

// ── 11. HouseholdBudget ───────────────────────────────────────
// SOURCE: gold.household_budgets table
// USED BY: Budget-aware grocery recommendations and substitutions
MERGE (hb:HouseholdBudget {id: $id})
SET hb.household_id = $householdId,
    hb.weekly_amount = $weeklyAmount,
    hb.currency = $currency

// ── 12. HealthProfile ─────────────────────────────────────────
// SOURCE: gold.b2c_customer_health_profiles table
// USED BY: Nutritional gap detection — "you're averaging 42g protein vs 60g target"
MERGE (hp:HealthProfile {id: $id})
SET hp.b2c_customer_id = $customerId,
    hp.member_id = $memberId,
    hp.calorie_target = $calorieTarget,
    hp.protein_target_g = $proteinTarget,
    hp.carb_target_g = $carbTarget,
    hp.fat_target_g = $fatTarget

// ── 13. MealLogTemplate ───────────────────────────────────────
// SOURCE: gold.meal_log_templates table
// USED BY: Chatbot "log meal" — recognize repeated meal patterns
MERGE (mlt:MealLogTemplate {id: $id})
SET mlt.b2c_customer_id = $customerId,
    mlt.name = $name,
    mlt.meal_type = $mealType,
    mlt.template_data = $templateData

// ── 14. MealLogStreak ─────────────────────────────────────────
// SOURCE: gold.meal_log_streaks table
// USED BY: Gamification — "You've logged meals for 14 days straight!"
MERGE (mls:MealLogStreak {id: $id})
SET mls.b2c_customer_id = $customerId,
    mls.current_streak = $currentStreak,
    mls.longest_streak = $longestStreak,
    mls.last_log_date = date($lastLogDate)
```

### Step 4: 22 New Relationship Types (Created by Sync Script)

```cypher
// ── MEAL PLAN RELATIONSHIPS ───────────────────────────────────

// 1. Customer → MealPlan
// WHY: "Show me my meal plans" traverses this edge
MATCH (c:B2CCustomer {id: $customerId}), (mp:MealPlan {id: $mealPlanId})
MERGE (c)-[:HAS_PLAN]->(mp)

// 2. MealPlan → MealPlanItem
// WHY: Iterate items in a plan for swap suggestions
MATCH (mp:MealPlan {id: $mealPlanId}), (mpi:MealPlanItem {id: $itemId})
MERGE (mp)-[:CONTAINS_ITEM]->(mpi)

// 3. MealPlanItem → Recipe
// WHY: Link planned meals to actual recipe nodes for nutrition scoring
MATCH (mpi:MealPlanItem {id: $itemId}), (r:Recipe {id: $recipeId})
MERGE (mpi)-[:PLANS_RECIPE]->(r)

// ── MEAL LOG RELATIONSHIPS ────────────────────────────────────

// 4. Customer → MealLog
// WHY: Pattern analysis starts here — "what did this customer eat?"
MATCH (c:B2CCustomer {id: $customerId}), (ml:MealLog {id: $logId})
MERGE (c)-[:LOGGED_MEAL]->(ml)

// 5. MealLog → MealLogItem
// WHY: Break down a day's log into individual meals
MATCH (ml:MealLog {id: $logId}), (mli:MealLogItem {id: $itemId})
MERGE (ml)-[:CONTAINS_ITEM]->(mli)

// 6. MealLogItem → Recipe (what was eaten)
// WHY: Connect logged meals to recipe nodes for nutrition/variety analysis
MATCH (mli:MealLogItem {id: $itemId}), (r:Recipe {id: $recipeId})
MERGE (mli)-[:OF_RECIPE]->(r)

// 7. MealLogItem → Product (scanned product eaten as meal)
// WHY: Some users log scanned products as meals instead of recipes
MATCH (mli:MealLogItem {id: $itemId}), (p:Product {id: $productId})
MERGE (mli)-[:OF_PRODUCT]->(p)

// ── SHOPPING RELATIONSHIPS ────────────────────────────────────

// 8. Customer → ShoppingList
// WHY: "Show me my shopping list" traverses this edge
MATCH (c:B2CCustomer {id: $customerId}), (sl:ShoppingList {id: $listId})
MERGE (c)-[:HAS_LIST]->(sl)

// 9. ShoppingList → MealPlan (derived from)
// WHY: Trace which meal plan generated this shopping list
MATCH (sl:ShoppingList {id: $listId}), (mp:MealPlan {id: $mealPlanId})
MERGE (sl)-[:DERIVED_FROM]->(mp)

// 10. ShoppingList → ShoppingListItem
// WHY: Iterate items for product substitution recommendations
MATCH (sl:ShoppingList {id: $listId}), (sli:ShoppingListItem {id: $itemId})
MERGE (sl)-[:CONTAINS_ITEM]->(sli)

// 11. ShoppingListItem → Product
// WHY: Link shopping items to product nodes for price/allergen lookups
MATCH (sli:ShoppingListItem {id: $itemId}), (p:Product {id: $productId})
MERGE (sli)-[:OF_PRODUCT]->(p)

// ── INTERACTION RELATIONSHIPS (critical for collaborative filtering) ──

// 12. Customer -[:RATED]-> Recipe
// WHY: Core signal for collaborative filtering — "users who rated X highly"
MATCH (c:B2CCustomer {id: $customerId}), (r:Recipe {id: $recipeId})
MERGE (c)-[rel:RATED]->(r)
SET rel.rating = $rating, rel.created_at = datetime($createdAt)

// 13. Customer -[:SAVED]-> Recipe (bookmarked)
// WHY: Implicit positive signal — saved = interested
MATCH (c:B2CCustomer {id: $customerId}), (r:Recipe {id: $recipeId})
MERGE (c)-[rel:SAVED]->(r)
SET rel.saved_at = datetime($savedAt)

// 14. Customer -[:VIEWED]-> Recipe
// WHY: Weakest signal but high volume — used when RATED/SAVED data is sparse
MATCH (c:B2CCustomer {id: $customerId}), (r:Recipe {id: $recipeId})
MERGE (c)-[rel:VIEWED]->(r)
SET rel.viewed_at = datetime($viewedAt), rel.view_count = $viewCount

// 15. Customer -[:SCANNED]-> Product
// WHY: Scanner history — "this user frequently scans dairy products"
MATCH (c:B2CCustomer {id: $customerId}), (p:Product {id: $productId})
MERGE (c)-[rel:SCANNED]->(p)
SET rel.scanned_at = datetime($scannedAt)

// ── HOUSEHOLD RELATIONSHIPS ───────────────────────────────────

// 16. Customer → Household
// WHY: Multi-member meal plans need to know which household the user belongs to
MATCH (c:B2CCustomer {id: $customerId}), (h:Household {id: $householdId})
MERGE (c)-[:BELONGS_TO_HOUSEHOLD]->(h)

// 17. Household → HouseholdMember
// WHY: Each member may have different allergens/diets to respect in planning
MATCH (h:Household {id: $householdId}), (hm:HouseholdMember {id: $memberId})
MERGE (h)-[:HAS_MEMBER]->(hm)

// 18. Household → HouseholdBudget
// WHY: Budget-aware grocery substitutions need to know the household budget
MATCH (h:Household {id: $householdId}), (hb:HouseholdBudget {id: $budgetId})
MERGE (h)-[:HAS_BUDGET]->(hb)

// ── HEALTH PROFILE RELATIONSHIPS ──────────────────────────────

// 19. Customer → HealthProfile
// WHY: Nutritional gap detection needs calorie/protein/carb/fat targets
MATCH (c:B2CCustomer {id: $customerId}), (hp:HealthProfile {id: $profileId})
MERGE (c)-[:HAS_PROFILE]->(hp)

// 20. HouseholdMember → HealthProfile
// WHY: Children/dependents in a household have their own nutrition targets
MATCH (hm:HouseholdMember {id: $memberId}), (hp:HealthProfile {id: $profileId})
MERGE (hm)-[:HAS_PROFILE]->(hp)

// ── SUBSTITUTION RELATIONSHIPS ────────────────────────────────

// 21. Product -[:CAN_SUBSTITUTE]-> Product
// WHY: Scanner alternatives + grocery substitutions query this edge
// This enables: "Scanned product X contains your allergen → try product Y instead"
MATCH (p1:Product {id: $originalId}), (p2:Product {id: $substituteId})
MERGE (p1)-[rel:CAN_SUBSTITUTE]->(p2)
SET rel.reason = $reason, rel.savings = $savings

// 22. Ingredient -[:CAN_SUBSTITUTE]-> Ingredient
// WHY: Recipe-level substitutions: "Can I use almond flour instead of wheat flour?"
MATCH (i1:Ingredient {id: $originalId}), (i2:Ingredient {id: $substituteId})
MERGE (i1)-[rel:CAN_SUBSTITUTE]->(i2)
SET rel.reason = $reason
```

### Schema Changes Summary

| Category               | Count | Purpose                                                                                  |
| ---------------------- | ----- | ---------------------------------------------------------------------------------------- |
| Uniqueness constraints | 14    | Prevent duplicate nodes during sync                                                      |
| Performance indexes    | 7     | Speed up date range, customer lookup, and rating queries                                 |
| New node types         | 14    | MealPlan, MealLog, ShoppingList, RecipeRating, ScanEvent, Household, HealthProfile, etc. |
| New relationship types | 22    | HAS\_PLAN, LOGGED\_MEAL, HAS\_LIST, RATED, SAVED, VIEWED, SCANNED, CAN\_SUBSTITUTE, etc. |

***

## 10. Change 8: GraphSAGE Automation

### \[NEW] `sync/train_graphsage.py`

Automate the weekly GraphSAGE training that's currently run manually.

***

## 11. Environment Variables

```shellscript
# Neo4j
NEO4J_URI=bolt://neo4j:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=<secure-password>
NEO4J_DATABASE=neo4j

# LLM
OPENAI_API_KEY=<key>
OPENAI_BASE_URL=<litellm-proxy-url>
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
INTENT_MODEL=gpt-4o-mini
GENERATION_MODEL=openai/gpt-5-mini

# RAG API
RAG_API_KEY=<shared-secret-with-express>
ALLOWED_ORIGINS=http://express-backend:5000
EMBEDDING_CONFIG=embedding_config.yaml

# Sync (only needed for pg_sync.py)
PG_READ_URL=postgresql://<read-only-user>:<password>@<supabase-host>:5432/postgres
```

***

## 12. Testing Checklist

| Test             | Command                                                   | Expected                                 |
| ---------------- | --------------------------------------------------------- | ---------------------------------------- |
| Health           | `curl http://localhost:8000/health`                       | `{"status": "ok", "neo4j": "connected"}` |
| Search           | `POST /search/hybrid {"query": "vegan breakfast"}`        | Returns recipe IDs + scores              |
| Feed             | `POST /recommend/feed {"customer_id": "..."}`             | Returns personalized recipes             |
| Chat (greeting)  | `POST /chat/process {"message": "hi"}`                    | NLU source = "rules", no LLM call        |
| Chat (search)    | `POST /chat/process {"message": "find me a keto recipe"}` | Returns recipes from graph               |
| Chat (meal plan) | `POST /chat/process {"message": "plan my meals"}`         | Returns `action_required: true`          |
| Auth             | `POST /search/hybrid` without header                      | Returns 401                              |
| Invalid key      | `POST /search/hybrid` with wrong key                      | Returns 401                              |
