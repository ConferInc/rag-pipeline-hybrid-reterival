"""
FastAPI application wrapping the RAG pipeline for B2C frontend integration.

Architecture:
  Express Backend --HTTP/REST--> This API --> Neo4j + LLM
  (Handles user auth)            (Handles retrieval + generation)

Dependency direction: api → rag_pipeline (one-way). Core pipeline is untouched.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from neo4j import Driver
from openai import OpenAI
from pydantic import BaseModel, Field

from rag_pipeline.augmentation.fusion import apply_rrf
from rag_pipeline.config import load_embedding_config
from rag_pipeline.embeddings.caching_embedder import CachingQueryEmbedder
from rag_pipeline.embeddings.openai_embedder import OpenAIQueryEmbedder
from rag_pipeline.neo4j_client import create_neo4j_driver, neo4j_settings_from_env
from rag_pipeline.orchestrator.cypher_runner import run_cypher_retrieval
from rag_pipeline.orchestrator.orchestrator import orchestrate, OrchestratorResult
from rag_pipeline.retrieval.service import retrieve_semantic, SemanticRetrievalRequest
from rag_pipeline.retrieval.structural import get_seed_embedding, structural_search_with_expansion

from chatbot.action_orchestrator import (
    is_confirmation_message,
    is_rejection_message,
    route_intent,
)
from chatbot.chatbot_cypher import (
    format_meal_history_response,
    format_meal_plan_response,
    format_nutrition_summary_response,
    run_meal_history,
    run_nutrition_summary,
    run_show_meal_plan,
)
from chatbot.nlu import extract_hybrid
from chatbot.response_generator import (
    TEMPLATE_INTENTS,
    format_conversation_history,
    generate_chat_response,
    get_template_response,
)
from chatbot.session import get_or_create_session, cleanup_expired

logger = logging.getLogger(__name__)

# Intents that run retrieval + LLM (same flow as /search/hybrid)
DATA_INTENTS_NEEDING_RETRIEVAL = frozenset({
    "find_recipe",
    "find_recipe_by_pantry",
    "get_nutritional_info",
    "compare_foods",
    "check_diet_compliance",
    "check_substitution",
    "get_substitution_suggestion",
    "similar_recipes",
    "recipes_for_cuisine",
    "recipes_by_nutrient",
    "nutrient_in_foods",
    "nutrient_category",
    "ingredient_in_recipes",
    "ingredient_nutrients",
    "find_product",
    "product_nutrients",
    "cuisine_recipes",
    "cuisine_hierarchy",
    "cross_reactive_allergens",
    "general_nutrition",
})

# Deterministic chatbot intents: fixed Cypher, no LLM
CHATBOT_DATA_INTENTS = frozenset({
    "show_meal_plan",
    "meal_history",
    "nutrition_summary",
})


# ── Startup / Shutdown ─────────────────────────────────────────────────────

load_dotenv()

_driver = None
_cfg = None
_embedder = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize Neo4j driver + embedder on startup, close on shutdown."""
    global _driver, _cfg, _embedder

    neo_settings = neo4j_settings_from_env()
    _driver = create_neo4j_driver(neo_settings)
    config_path = os.getenv("EMBEDDING_CONFIG", "embedding_config.yaml")
    _cfg = load_embedding_config(config_path)
    base_embedder = OpenAIQueryEmbedder(
        client=OpenAI(
            base_url=os.getenv("OPENAI_BASE_URL"),
            api_key=os.getenv("OPENAI_API_KEY"),
        ),
        model=os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"),
    )
    try:
        with open(Path(config_path)) as f:
            raw_cfg = yaml.safe_load(f)
        cache_cfg = (raw_cfg or {}).get("embedding_cache", {}) or {}
    except Exception:
        cache_cfg = {}
    if cache_cfg.get("enabled", False):
        _embedder = CachingQueryEmbedder(
            base_embedder,
            max_size=cache_cfg.get("max_size", 500),
            key_normalize=cache_cfg.get("key_normalize", "strip_lower"),
        )
    else:
        _embedder = base_embedder

    yield

    if _driver:
        _driver.close()


app = FastAPI(
    title="NutriB2C RAG API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("ALLOWED_ORIGINS", "http://localhost:5000").split(","),
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)


# ── Auth ───────────────────────────────────────────────────────────────────

async def verify_api_key(x_api_key: str = Header(..., alias="X-API-Key")):
    """
    Service-to-service authentication.
    Express backend validates user JWTs, then calls us with this shared API key.
    """
    expected = os.getenv("RAG_API_KEY")
    if not expected or x_api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ── Request/Response Schemas ───────────────────────────────────────────────

class SearchRequest(BaseModel):
    """Natural language search query from the B2C search page."""
    query: str = Field(..., max_length=500, description="User's search text")
    customer_id: str | None = Field(None, description="B2C customer UUID (for personalization)")
    filters: dict[str, Any] = Field(default_factory=dict, description="Structured filters")
    limit: int = Field(20, ge=1, le=50)


class FeedRequest(BaseModel):
    """Personalized recipe feed — no user query needed, driven by customer profile."""
    customer_id: str = Field(..., description="B2C customer UUID (required for personalization)")
    meal_type: str | None = Field(None, description="Optional meal type hint: breakfast/lunch/dinner/snack")
    limit: int = Field(20, ge=1, le=50)


class MealCandidateRequest(BaseModel):
    """
    Pre-scored recipe candidates for meal plan generation.
    Single customer only — uses profile from customer_id.
    """
    customer_id: str = Field(..., description="B2C customer UUID")
    meal_history: list[str] = Field(default_factory=list, description="Recipe IDs to exclude (e.g. from PostgreSQL meal_logs)")
    meal_type: str | None = Field(None, description="Optional: breakfast/lunch/dinner/snack")
    exclude_ids: list[str] = Field(default_factory=list, description="Additional recipe IDs to exclude (e.g. for swap)")
    limit: int = Field(50, ge=1, le=100)


class MealCandidateItem(BaseModel):
    """Single recipe candidate with score and reasons."""
    recipe_id: str
    title: str
    score: float
    reasons: list[str] = Field(default_factory=list)


class MealCandidateResponse(BaseModel):
    candidates: list[MealCandidateItem]
    retrieval_time_ms: float


class RecommendationResult(BaseModel):
    """Single recommendation with explainability."""
    id: str
    score: float
    reasons: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchResponse(BaseModel):
    results: list[RecommendationResult]
    intent: str
    entities: dict[str, Any]
    retrieval_time_ms: float


# ── Chatbot schemas ────────────────────────────────────────────────────────

class ChatProcessRequest(BaseModel):
    """Chatbot message from Express (POST /chat/process)."""
    message: str = Field(..., min_length=1, max_length=500, description="User message")
    customer_id: str = Field(..., description="B2C customer UUID")
    session_id: str | None = Field(None, description="Existing session UUID (null for new session)")
    display_name: str | None = Field(None, description="Customer name from auth/PostgreSQL if Neo4j has none")


class ChatRecipeItem(BaseModel):
    """Recipe returned in chat response (e.g. find_recipe intent)."""
    id: str
    title: str
    score: float = 0.0


class PendingActionResponse(BaseModel):
    """Action awaiting user confirmation."""
    type: str = Field(..., description="Action type, e.g. log_meal, plan_meals")
    params: dict[str, Any] = Field(default_factory=dict, description="Action parameters")
    action_id: str | None = Field(None, description="Unique ID for matching confirmation")


class ChatProcessResponse(BaseModel):
    """Chatbot response to Express."""
    response: str = Field(..., description="Natural language or template response")
    intent: str = Field(..., description="Detected intent")
    session_id: str = Field(..., description="Session UUID for follow-up messages")
    message_count: int = Field(0, ge=0, description="Messages in this session")
    action_required: bool = Field(False, description="True if user must confirm before execution")
    confirmation_prompt: str | None = Field(None, description="Prompt shown for confirmation")
    pending_action: PendingActionResponse | None = Field(None, description="Action to confirm")
    action_to_execute: PendingActionResponse | None = Field(
        None,
        description="When user confirmed: action for Express to execute",
    )
    recipes: list[ChatRecipeItem] = Field(default_factory=list, description="Recipe results if any")
    nutrition_data: dict[str, Any] | None = Field(None, description="Nutrition info if any")


# ── Profile helpers (feed / meal-candidates) ───────────────────────────────

def fetch_customer_profile(
    driver: Driver,
    customer_id: str,
    database: str | None = None,
) -> dict[str, Any]:
    """
    Fetch all personalization signals for a customer from Neo4j in one query.

    Returns a dict with:
      display_name      — customer name (display_name, full_name, or name from B2C_Customer)
      diets             — list of Dietary_Preferences names the customer follows
      allergens         — list of Allergens names the customer is allergic to
      health_conditions — list of B2C_Customer_Health_Conditions names (e.g. "Type 2 Diabetes")
      health_goal       — string from B2C_Customer_Health_Profiles (e.g. "weight_loss")
      activity_level    — string from B2C_Customer_Health_Profiles (e.g. "active")
      recent_recipes    — list of Recipe titles eaten in the last 14 days
                         (requires HAS_MEAL_LOG → CONTAINS_ITEM → OF_RECIPE to be populated)

    NOTE: allergens are used as exclude_ingredient in Cypher retrieval. This is an
    approximation — it works when allergen names match Ingredient node names exactly.
    The proper fix (FORBIDS relationship from Dietary_Preferences → Ingredient) is a
    future data task.
    """
    cypher = """
    MATCH (c:B2C_Customer)
    WHERE c.id = $customer_id OR elementId(c) = $customer_id
    OPTIONAL MATCH (c)-[:FOLLOWS_DIET]->(dp:Dietary_Preferences)
    OPTIONAL MATCH (c)-[:IS_ALLERGIC]->(a:Allergens)
    OPTIONAL MATCH (c)-[:HAS_CONDITION]->(hc:B2C_Customer_Health_Conditions)
    OPTIONAL MATCH (c)-[:HAS_PROFILE]->(hp:B2C_Customer_Health_Profiles)
    OPTIONAL MATCH (c)-[:HAS_MEAL_LOG]->(ml:MealLog)
                   -[:CONTAINS_ITEM]->(mli:MealLogItem)
                   -[:OF_RECIPE]->(r:Recipe)
    WHERE (ml IS NULL OR ml.log_date >= date() - duration({days: 14}))
    RETURN
      coalesce(c.display_name, c.full_name, c.name) AS display_name,
      collect(DISTINCT dp.name)  AS diets,
      collect(DISTINCT a.name)   AS allergens,
      collect(DISTINCT hc.name)  AS health_conditions,
      hp.health_goal             AS health_goal,
      hp.activity_level          AS activity_level,
      collect(DISTINCT r.title)  AS recent_recipes
    """
    try:
        with driver.session(database=database) as session:
            record = session.run(cypher, customer_id=customer_id).single()
            if not record:
                logger.warning("fetch_customer_profile: no record for customer_id=%s", customer_id)
                return {
                    "display_name": None,
                    "diets": [], "allergens": [], "health_conditions": [],
                    "health_goal": None, "activity_level": None, "recent_recipes": [],
                }
            name = record["display_name"]
            if isinstance(name, str) and name.strip():
                name = name.strip()
            else:
                name = None
            return {
                "display_name":    name,
                "diets":           list(record["diets"] or []),
                "allergens":       list(record["allergens"] or []),
                "health_conditions": list(record["health_conditions"] or []),
                "health_goal":     record["health_goal"],
                "activity_level":  record["activity_level"],
                "recent_recipes":  list(record["recent_recipes"] or []),
            }
    except Exception as e:
        logger.warning("fetch_customer_profile failed: %s", e)
        return {
            "display_name": None,
            "diets": [], "allergens": [], "health_conditions": [],
            "health_goal": None, "activity_level": None, "recent_recipes": [],
        }


_GOAL_WORDS: dict[str, str] = {
    "weight_loss":   "low calorie",
    "muscle_gain":   "high protein",
    "heart_health":  "low fat",
    "energy":        "energizing",
    "general_health": "healthy",
}


def build_feed_query_text(profile: dict[str, Any], meal_type: str | None = None) -> str:
    """
    Build a short text string for the semantic embedder from a customer profile.
    This text is NOT passed to the LLM — it is only embedded for vector similarity search.

    Examples:
      diets=["Vegan"], goal="weight_loss"  → "Vegan low calorie recipes"
      diets=[],        goal="muscle_gain"  → "high protein recipes"
      meal_type="breakfast"                → "Vegan low calorie breakfast recipes"
    """
    parts: list[str] = list(profile.get("diets") or [])
    goal_text = _GOAL_WORDS.get(profile.get("health_goal") or "", "healthy")
    parts.append(goal_text)
    if meal_type:
        parts.append(meal_type)
    parts.append("recipes")
    return " ".join(parts)


# ── Reason generation ──────────────────────────────────────────────────────

def _build_reasons(
    item: dict[str, Any],
    entities: dict[str, Any],
    profile: dict[str, Any] | None = None,
) -> list[str]:
    """
    Generate 1–3 short human-readable reasons for a recommendation.

    Works for both personalized (profile provided) and search (profile=None) contexts.
    Reasons are derived from retrieval sources and payload properties — no extra DB calls.
    """
    reasons: list[str] = []
    sources = item.get("sources", [])
    payload = item.get("payload", {}) or {}

    # Source-based reasons — personalized when profile available
    if "cypher" in sources:
        diets = (profile or {}).get("diets") or entities.get("diet") or []
        if diets:
            reasons.append(f"Matches your {diets[0]} diet")
        else:
            reasons.append("Matches your query criteria")

    if "structural" in sources:
        reasons.append("Popular with people like you")

    if "semantic" in sources and "cypher" not in sources:
        if profile:
            reasons.append("Closely matches your preferences")
        else:
            reasons.append("Semantically similar to your query")

    # Payload-based reasons (available from Cypher RETURN columns)
    time_mins = payload.get("total_time_minutes")
    if time_mins and isinstance(time_mins, (int, float)) and time_mins <= 30:
        reasons.append("Ready in under 30 minutes")

    pct_protein = payload.get("percent_calories_protein")
    if pct_protein and isinstance(pct_protein, (int, float)) and pct_protein >= 30:
        reasons.append("High in protein")

    if not reasons:
        reasons.append("Relevant match")

    return reasons[:3]


# ── Merge helpers ──────────────────────────────────────────────────────────

def _resolve_id(payload: dict[str, Any], key: str) -> str:
    """Resolve the best available recipe ID from a fused result payload."""
    return (
        payload.get("id")
        or payload.get("r.id")
        or key
    ) or str(id(payload))


def _merge_results_with_profile(
    fused: list[dict[str, Any]],
    entities: dict[str, Any],
    profile: dict[str, Any],
    *,
    limit: int = 20,
) -> list[RecommendationResult]:
    """
    Build RecommendationResult list from raw fused RRF results for personalized endpoints.
    Used by /recommend/feed and /recommend/meal-candidates (bypasses OrchestratorResult).
    """
    out: list[RecommendationResult] = []
    for item in fused[:limit]:
        payload = item.get("payload", {}) or {}
        key     = item.get("key", "")
        title   = item.get("title") or payload.get("title") or payload.get("name") or key
        rec_id  = _resolve_id(payload, key)

        out.append(
            RecommendationResult(
                id=str(rec_id),
                score=float(item.get("rrf_score", 0.0)),
                reasons=_build_reasons(item, entities, profile),
                metadata={
                    "title":     title,
                    "label":     item.get("label", ""),
                    "sources":   item.get("sources", []),
                    "id_source": "uuid" if payload.get("id") else "title_key",
                },
            )
        )
    return out


# ── Helpers ────────────────────────────────────────────────────────────────

def _merge_results(orch: OrchestratorResult, *, limit: int = 20) -> list[RecommendationResult]:
    """
    Merge fused RRF results into a ranked list for API response.
    Uses OrchestratorResult.fused_results (already RRF-fused by apply_rrf()).

    ID resolution priority:
      1. payload["id"]      — UUID written by fusion.py from r.id (Cypher results)
      2. payload["r.id"]    — raw Cypher column fallback
      3. key                — fusion key: UUID if Cypher ran, normalized title if semantic-only
    The Express backend uses this ID to hydrate full recipe data from PostgreSQL.
    When only semantic results are present (no Cypher match), the key is a normalized
    title — Express should handle this as a title-based lookup until Cypher gaps are fixed.
    """
    fused = orch.fused_results[:limit]
    out: list[RecommendationResult] = []

    for item in fused:
        payload = item.get("payload", {}) or {}
        key     = item.get("key", "")
        title   = item.get("title") or payload.get("title") or payload.get("name") or key
        rec_id  = _resolve_id(payload, key)

        out.append(
            RecommendationResult(
                id=str(rec_id),
                score=float(item.get("rrf_score", 0.0)),
                reasons=_build_reasons(item, orch.entities, profile=None),
                metadata={
                    "title":     title,
                    "label":     item.get("label", ""),
                    "sources":   item.get("sources", []),
                    "id_source": "uuid" if payload.get("id") else "title_key",
                },
            )
        )

    return out


# ── Endpoints ──────────────────────────────────────────────────────────────

@app.get("/debug/profile", dependencies=[Depends(verify_api_key)])
async def debug_profile(customer_id: str):
    """Debug: return raw profile for a customer_id to verify Neo4j lookup."""
    database = os.getenv("NEO4J_DATABASE")
    profile = fetch_customer_profile(_driver, customer_id, database)
    return {"customer_id": customer_id, "profile": profile}


@app.get("/health")
async def health():
    """Health check — load balancers use this to verify the service is alive."""
    try:
        _driver.verify_connectivity()
        return {"status": "ok", "neo4j": "connected"}
    except Exception as e:
        return {"status": "degraded", "neo4j": str(e)}


@app.post("/search/hybrid", response_model=SearchResponse, dependencies=[Depends(verify_api_key)])
async def search_hybrid(req: SearchRequest):
    """
    Natural language search with hybrid retrieval.

    Flow: NLU → profile fetch → entity merge → semantic + structural + cypher → RRF fusion → ranked results.
    When customer_id is present the customer's stored diets, allergens, and health conditions
    are silently merged into the extracted entities before retrieval so personalisation
    constraints are always enforced without the user having to repeat them.
    Express backend hydrates recipe IDs with full data from PostgreSQL.
    """
    start    = time.time()
    database = os.getenv("NEO4J_DATABASE")

    # Fetch profile once if the user is logged in; None for guest/anonymous searches.
    customer_profile = (
        fetch_customer_profile(_driver, req.customer_id, database)
        if req.customer_id
        else None
    )

    result = await orchestrate(
        _driver,
        cfg=_cfg,
        embedder=_embedder,
        user_query=req.query,
        customer_node_id=req.customer_id,
        customer_profile=customer_profile,
        top_k=req.limit,
        database=database,
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
    Personalized recipe feed driven entirely by customer profile.

    Bypasses LLM intent extraction — intent is always find_recipe.
    Flow:
      1. Fetch customer profile from Neo4j (diets, allergens, health_goal, recent_recipes)
      2. Build structured entities directly from profile (no LLM)
      3. Build synthetic text for semantic embedder only
      4. Run semantic + structural + Cypher retrievals in parallel-equivalent calls
      5. RRF fusion
      6. Post-filter recently eaten recipes (last 14 days)
      7. Return ranked results with personalized reasons

    NOTE: allergen exclusion maps allergen names → exclude_ingredient. This works when
    allergen names match Ingredient node names. Full fix requires FORBIDS relationships.
    """
    start    = time.time()
    database = os.getenv("NEO4J_DATABASE")

    profile = fetch_customer_profile(_driver, req.customer_id, database)

    entities: dict[str, Any] = {
        "diet":               profile["diets"],
        "exclude_ingredient": profile["allergens"],
    }
    if req.meal_type:
        entities["course"] = req.meal_type

    synthetic_text = build_feed_query_text(profile, req.meal_type)

    # ── Semantic retrieval ────────────────────────────────────────────────
    semantic_results: list[Any] = []
    try:
        semantic_results = retrieve_semantic(
            _driver,
            cfg=_cfg,
            embedder=_embedder,
            request=SemanticRetrievalRequest(
                query=synthetic_text,
                top_k=req.limit,
                label="Recipe",
            ),
            database=database,
        )
    except Exception as e:
        logger.warning("recommend_feed: semantic retrieval failed: %s", e)

    # ── Structural retrieval ──────────────────────────────────────────────
    structural_results: dict[str, Any] = {}
    try:
        seed_emb = get_seed_embedding(
            _driver,
            cfg=_cfg,
            label="B2C_Customer",
            node_id=req.customer_id,
            database=database,
        )
        if seed_emb:
            structural_results = structural_search_with_expansion(
                _driver,
                cfg=_cfg,
                label="B2C_Customer",
                seed_vector=seed_emb,
                top_k=req.limit,
                allowed_labels=["Recipe"],
                allowed_relationships=["SAVED", "VIEWED"],
                database=database,
            )
    except Exception as e:
        logger.warning("recommend_feed: structural retrieval failed: %s", e)

    # ── Cypher retrieval ──────────────────────────────────────────────────
    cypher_results: list[dict[str, Any]] = []
    try:
        cypher_results = run_cypher_retrieval(
            _driver,
            intent="find_recipe",
            entities=entities,
            database=database,
        )
    except Exception as e:
        logger.warning("recommend_feed: cypher retrieval failed: %s", e)

    # ── RRF fusion ────────────────────────────────────────────────────────
    fused = apply_rrf(
        semantic_results,
        structural_results,
        cypher_results,
        "find_recipe",
        max_items=req.limit,
    )

    # ── Post-filter: remove recipes eaten in the last 14 days ─────────────
    recent = {t.lower() for t in profile["recent_recipes"]}
    if recent:
        fused = [f for f in fused if (f.get("title") or "").lower() not in recent]

    recommendations = _merge_results_with_profile(fused, entities, profile, limit=req.limit)

    return SearchResponse(
        results=recommendations,
        intent="find_recipe",
        entities=entities,
        retrieval_time_ms=(time.time() - start) * 1000,
    )


@app.post("/recommend/meal-candidates", response_model=MealCandidateResponse, dependencies=[Depends(verify_api_key)])
async def recommend_meal_candidates(req: MealCandidateRequest):
    """
    Pre-scored recipe candidates for meal plan generation (single customer).

    Uses the same retrieval as /recommend/feed but excludes recipes in meal_history
    and exclude_ids. Express backend can pass meal_history from PostgreSQL meal_logs
    (recipe IDs eaten recently) since Neo4j MealLog may not be populated yet.

    Flow:
      1. Fetch customer profile (diets, allergens, recent_recipes)
      2. Build entities, run semantic + structural + Cypher + RRF
      3. Post-filter: exclude recent_recipes, meal_history, exclude_ids
      4. Return candidates in format expected by meal plan LLM
    """
    start = time.time()
    database = os.getenv("NEO4J_DATABASE")

    profile = fetch_customer_profile(_driver, req.customer_id, database)

    entities: dict[str, Any] = {
        "diet": profile["diets"],
        "exclude_ingredient": profile["allergens"],
    }
    if req.meal_type:
        entities["course"] = req.meal_type

    synthetic_text = build_feed_query_text(profile, req.meal_type)

    # ── Semantic retrieval ────────────────────────────────────────────────
    semantic_results: list[Any] = []
    try:
        semantic_results = retrieve_semantic(
            _driver,
            cfg=_cfg,
            embedder=_embedder,
            request=SemanticRetrievalRequest(
                query=synthetic_text,
                top_k=req.limit,
                label="Recipe",
            ),
            database=database,
        )
    except Exception as e:
        logger.warning("recommend_meal_candidates: semantic retrieval failed: %s", e)

    # ── Structural retrieval ──────────────────────────────────────────────
    structural_results: dict[str, Any] = {}
    try:
        seed_emb = get_seed_embedding(
            _driver,
            cfg=_cfg,
            label="B2C_Customer",
            node_id=req.customer_id,
            database=database,
        )
        if seed_emb:
            structural_results = structural_search_with_expansion(
                _driver,
                cfg=_cfg,
                label="B2C_Customer",
                seed_vector=seed_emb,
                top_k=req.limit,
                allowed_labels=["Recipe"],
                allowed_relationships=["SAVED", "VIEWED"],
                database=database,
            )
    except Exception as e:
        logger.warning("recommend_meal_candidates: structural retrieval failed: %s", e)

    # ── Cypher retrieval ──────────────────────────────────────────────────
    cypher_results: list[dict[str, Any]] = []
    try:
        cypher_results = run_cypher_retrieval(
            _driver,
            intent="find_recipe",
            entities=entities,
            database=database,
        )
    except Exception as e:
        logger.warning("recommend_meal_candidates: cypher retrieval failed: %s", e)

    # ── RRF fusion ────────────────────────────────────────────────────────
    fused = apply_rrf(
        semantic_results,
        structural_results,
        cypher_results,
        "find_recipe",
        max_items=req.limit,
    )

    # ── Post-filter: exclude recently eaten + meal_history + exclude_ids ───
    exclude_ids = {rid.strip().lower() for rid in (req.meal_history or []) + (req.exclude_ids or []) if rid}
    recent_titles = {t.lower() for t in profile["recent_recipes"] or []}

    def _should_exclude(item: dict[str, Any]) -> bool:
        payload = item.get("payload") or {}
        key = item.get("key", "")
        rec_id = _resolve_id(payload, key)
        title = (payload.get("title") or payload.get("name") or key or "").lower()
        if rec_id and str(rec_id).lower() in exclude_ids:
            return True
        if title in recent_titles:
            return True
        return False

    fused = [f for f in fused if not _should_exclude(f)]

    # ── Map to MealCandidateItem format ────────────────────────────────────
    recs = _merge_results_with_profile(fused, entities, profile, limit=req.limit)
    candidates = [
        MealCandidateItem(
            recipe_id=r.id,
            title=r.metadata.get("title", r.id),
            score=r.score,
            reasons=r.reasons,
        )
        for r in recs
    ]

    return MealCandidateResponse(
        candidates=candidates,
        retrieval_time_ms=(time.time() - start) * 1000,
    )


@app.post("/chat/process", response_model=ChatProcessResponse, dependencies=[Depends(verify_api_key)])
async def chat_process(req: ChatProcessRequest):
    """
    Chatbot message handler: NLU → intent routing → graph queries → LLM response.

    Called by Express POST /api/v1/chat. Session is created/reused for multi-turn context.
    NLU and response generation will be wired in Phase 3; currently returns a stub response.
    """
    # Get or create session and record user message
    session = get_or_create_session(req.customer_id, req.session_id)
    session.add_message("user", req.message.strip())
    cleanup_expired()

    msg = req.message.strip()

    # Confirmation flow: user said "yes" and we have a pending action
    if session.pending_action_payload and is_confirmation_message(msg):
        action_to_exec = session.pending_action_payload
        session.pending_action_payload = None
        session.pending_action = None
        session.add_message(
            "assistant",
            _confirmation_success_response(action_to_exec.get("type", "action")),
        )
        return ChatProcessResponse(
            response=_confirmation_success_response(action_to_exec.get("type", "action")),
            intent="confirmation",
            session_id=session.session_id,
            message_count=len(session.history),
            action_required=False,
            confirmation_prompt=None,
            pending_action=None,
            action_to_execute=PendingActionResponse(
                type=action_to_exec.get("type", "unknown"),
                params=action_to_exec.get("params", {}),
                action_id=action_to_exec.get("action_id"),
            ),
            recipes=[],
            nutrition_data=None,
        )

    # Rejection flow: user declined the pending action
    if session.pending_action_payload and is_rejection_message(msg):
        session.pending_action_payload = None
        session.pending_action = None
        session.add_message("assistant", "No problem, I won't do that.")
        return ChatProcessResponse(
            response="No problem, I won't do that.",
            intent="rejection",
            session_id=session.session_id,
            message_count=len(session.history),
            action_required=False,
            confirmation_prompt=None,
            pending_action=None,
            action_to_execute=None,
            recipes=[],
            nutrition_data=None,
        )

    # User sent something other than confirmation — clear any stale pending action
    if session.pending_action_payload and not is_confirmation_message(msg):
        session.pending_action_payload = None
        session.pending_action = None

    # NLU: intent + entities (rules first, LLM fallback)
    nlu_result = extract_hybrid(msg)

    # Action orchestrator: WRITE intents need confirmation
    orch = route_intent(nlu_result.intent, nlu_result.entities)
    if orch.action_required and orch.pending_action:
        session.pending_action_payload = orch.pending_action
        _response = orch.response_prefix or "Shall I go ahead?"
        return ChatProcessResponse(
            response=_response,
            intent=nlu_result.intent,
            session_id=session.session_id,
            message_count=len(session.history),
            action_required=True,
            confirmation_prompt=orch.confirmation_prompt,
            pending_action=PendingActionResponse(
                type=orch.pending_action.get("type", nlu_result.intent),
                params=orch.pending_action.get("params", {}),
                action_id=orch.pending_action.get("action_id"),
            ),
            action_to_execute=None,
            recipes=[],
            nutrition_data=None,
        )

    # Chatbot data intents — fixed Cypher, deterministic formatting, no LLM
    if nlu_result.intent in CHATBOT_DATA_INTENTS:
        try:
            database = os.getenv("NEO4J_DATABASE")
            if nlu_result.intent == "show_meal_plan":
                rows = run_show_meal_plan(_driver, req.customer_id, database)
                _response, _nutrition = format_meal_plan_response(rows)
            elif nlu_result.intent == "meal_history":
                rows = run_meal_history(_driver, req.customer_id, target_date=None, database=database)
                _response, _nutrition = format_meal_history_response(rows)
            elif nlu_result.intent == "nutrition_summary":
                data = run_nutrition_summary(_driver, req.customer_id, days=7, database=database)
                _response, _nutrition = format_nutrition_summary_response(data)
            else:
                _response = _stub_chat_response(nlu_result.intent)
                _nutrition = None
            session.add_message("assistant", _response)
            return ChatProcessResponse(
                response=_response,
                intent=nlu_result.intent,
                session_id=session.session_id,
                message_count=len(session.history),
                action_required=False,
                confirmation_prompt=None,
                pending_action=None,
                action_to_execute=None,
                recipes=[],
                nutrition_data=_nutrition,
            )
        except Exception as e:
            logger.exception("Chatbot data intent failed: %s", e)
            _response = "I couldn't load that data right now. Please try again later."
            session.add_message("assistant", _response)
            return ChatProcessResponse(
                response=_response,
                intent=nlu_result.intent,
                session_id=session.session_id,
                message_count=len(session.history),
                action_required=False,
                confirmation_prompt=None,
                pending_action=None,
                action_to_execute=None,
                recipes=[],
                nutrition_data=None,
            )

    # Template intents — canned response, no retrieval/LLM (fetch profile for personalization)
    if nlu_result.intent in TEMPLATE_INTENTS:
        database = os.getenv("NEO4J_DATABASE")
        profile = fetch_customer_profile(_driver, req.customer_id, database)
        display_name = profile.get("display_name") or req.display_name
        _response = get_template_response(
            nlu_result.intent,
            customer_name=display_name,
            profile=profile,
        )
        session.add_message("assistant", _response)
        return ChatProcessResponse(
            response=_response,
            intent=nlu_result.intent,
            session_id=session.session_id,
            message_count=len(session.history),
            action_required=False,
            confirmation_prompt=None,
            pending_action=None,
            action_to_execute=None,
            recipes=[],
            nutrition_data=None,
        )

    # Data intents — run retrieval (orchestrate) + LLM response generation
    if nlu_result.intent in DATA_INTENTS_NEEDING_RETRIEVAL:
        try:
            database = os.getenv("NEO4J_DATABASE")
            profile = fetch_customer_profile(_driver, req.customer_id, database)

            orch_result = await orchestrate(
                _driver,
                cfg=_cfg,
                embedder=_embedder,
                user_query=msg,
                customer_node_id=req.customer_id,
                customer_profile=profile,
                top_k=10,
                database=database,
            )

            # Build conversation history from session (exclude current user message)
            history_pairs = [
                (m.role, m.content)
                for m in session.history[:-1]
                if m.role in ("user", "assistant")
            ]
            history_text = format_conversation_history(history_pairs)

            _response = generate_chat_response(
                orch_result,
                msg,
                history_text,
                customer_profile=profile,
                temperature=0.3,
                max_fused=10,
            )
            session.add_message("assistant", _response)

            # Map fused results to ChatRecipeItem for recipe intents
            recipes_out: list[ChatRecipeItem] = []
            if orch_result.fused_results and nlu_result.intent in (
                "find_recipe",
                "find_recipe_by_pantry",
                "similar_recipes",
                "recipes_for_cuisine",
                "recipes_by_nutrient",
            ):
                recs = _merge_results(orch_result, limit=5)
                recipes_out = [
                    ChatRecipeItem(
                        id=r.id,
                        title=r.metadata.get("title", r.id),
                        score=r.score,
                    )
                    for r in recs
                ]

            return ChatProcessResponse(
                response=_response,
                intent=nlu_result.intent,
                session_id=session.session_id,
                message_count=len(session.history),
                action_required=False,
                confirmation_prompt=None,
                pending_action=None,
                action_to_execute=None,
                recipes=recipes_out,
                nutrition_data=None,
            )
        except Exception as e:
            logger.exception("Chat retrieval/generation failed: %s", e)
            _response = "I ran into an issue finding that. Please try again or rephrase your question."
            session.add_message("assistant", _response)
            return ChatProcessResponse(
                response=_response,
                intent=nlu_result.intent,
                session_id=session.session_id,
                message_count=len(session.history),
                action_required=False,
                confirmation_prompt=None,
                pending_action=None,
                action_to_execute=None,
                recipes=[],
                nutrition_data=None,
            )

    # Other intents (show_meal_plan, meal_history, etc.) — stub until wired
    _response = _stub_chat_response(nlu_result.intent)
    return ChatProcessResponse(
        response=_response,
        intent=nlu_result.intent,
        session_id=session.session_id,
        message_count=len(session.history),
        action_required=False,
        confirmation_prompt=None,
        pending_action=None,
        action_to_execute=None,
        recipes=[],
        nutrition_data=None,
    )


def _confirmation_success_response(action_type: str) -> str:
    """Response when user confirmed a pending action."""
    if action_type == "log_meal":
        return "Done! I've logged your meal."
    if action_type == "plan_meals":
        return "Your meal plan has been created!"
    if action_type == "swap_meal":
        return "I've swapped that meal for you."
    if action_type == "grocery_list":
        return "Added to your grocery list!"
    if action_type == "set_preference":
        return "I've updated your dietary preferences."
    return "Done!"


def _stub_chat_response(intent: str) -> str:
    """Placeholder for intents not yet wired (show_meal_plan, meal_history, etc.)."""
    return (
        f"I understood you want '{intent}'. This feature is coming soon. "
        "In the meantime, try asking for recipes or nutrition info!"
    )
