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
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Header, Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from neo4j import Driver
from openai import OpenAI
from pydantic import BaseModel, Field

from rag_pipeline.augmentation.fusion import apply_rrf
from rag_pipeline.config import load_embedding_config
from rag_pipeline.embeddings.caching_embedder import CachingQueryEmbedder
from rag_pipeline.embeddings.openai_embedder import OpenAIQueryEmbedder
from rag_pipeline.neo4j_client import create_neo4j_driver, neo4j_settings_from_env
from rag_pipeline.nlu.intents import CHATBOT_DATA_INTENTS, DATA_INTENTS_NEEDING_RETRIEVAL
from rag_pipeline.orchestrator.constraint_filter import apply_hard_constraints, build_zero_results_message
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
from chatbot.context_expander import expand_query_with_context
from chatbot.nlu import extract_hybrid
from chatbot.response_generator import (
    TEMPLATE_INTENTS,
    format_conversation_history,
    generate_chat_response,
    get_template_response,
)
from chatbot.session import get_or_create_session, cleanup_expired
from .ingredient_substitution import run_ingredient_substitution
from .product_recommendation import run_recommend_products, run_recommend_alternatives
from .notification_generator import generate_notification
from .rate_limit import check_rate_limit
from .b2b import router as b2b_router

logger = logging.getLogger(__name__)

# Config for security/abuse controls
_MAX_BODY_KB = int(os.getenv("MAX_REQUEST_BODY_KB", "64"))

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
    neo_timeout = os.getenv("NEO4J_CONNECTION_TIMEOUT")
    _driver = create_neo4j_driver(
        neo_settings,
        connection_timeout=float(neo_timeout) if neo_timeout else 10.0,
    )
    config_path = os.getenv("EMBEDDING_CONFIG", "embedding_config.yaml")
    _cfg = load_embedding_config(config_path)
    llm_timeout = float(os.getenv("LLM_TIMEOUT", "30"))
    base_embedder = OpenAIQueryEmbedder(
        client=OpenAI(
            base_url=os.getenv("OPENAI_BASE_URL"),
            api_key=os.getenv("OPENAI_API_KEY"),
            timeout=llm_timeout,
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


async def _request_body_size_middleware(request: Request, call_next):
    """Reject bodies larger than MAX_REQUEST_BODY_KB before processing."""
    if request.method == "POST":
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > _MAX_BODY_KB * 1024:
            return JSONResponse(
                status_code=413,
                content={"detail": "Your message is too long. Please shorten it and try again."},
            )
    return await call_next(request)


app.middleware("http")(_request_body_size_middleware)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Return user-friendly messages for security/validation errors."""
    _USER_FRIENDLY = {
        401: "Authentication failed. Please try again or sign in.",
        429: "You've sent too many requests. Please wait a minute and try again.",
    }
    detail = _USER_FRIENDLY.get(exc.status_code, exc.detail)
    if exc.status_code == 429:
        return JSONResponse(
            status_code=429,
            content={"detail": detail},
            headers={"Retry-After": "60", **(exc.headers or {})},
        )
    return JSONResponse(status_code=exc.status_code, content={"detail": detail})


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Return user-friendly message for validation errors (e.g. input too long)."""
    return JSONResponse(
        status_code=422,
        content={"detail": "Your input is invalid or too long. Please check and try again."},
    )


# ── Auth ───────────────────────────────────────────────────────────────────

async def verify_api_key(x_api_key: str = Header(..., alias="X-API-Key")):
    """
    Service-to-service authentication.
    Express backend validates user JWTs, then calls us with this shared API key.
    """
    expected = os.getenv("RAG_API_KEY")
    if not expected or x_api_key != expected:
        raise HTTPException(
            status_code=401,
            detail="Authentication failed. Please try again or sign in.",
        )


# B2B routes (vendor-scoped, same X-API-Key auth)
app.include_router(b2b_router, dependencies=[Depends(verify_api_key)])


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
    limit: int = Field(50, ge=1, le=50)


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
    zero_results_explanation: str | None = Field(None, description="Explanation when no candidates satisfy constraints")


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
    zero_results_explanation: str | None = Field(None, description="Explanation when no results satisfy constraints")
    confidence: float | None = Field(None, description="Intent extraction confidence (0–1)")


# ── Ingredient substitution schemas ────────────────────────────────────────

class IngredientSubstitutionRequest(BaseModel):
    """Request for POST /substitutions/ingredient."""
    ingredient_id: str = Field(..., description="Ingredient UUID or elementId")
    ingredient_name: str | None = Field(None, description="Ingredient name (optional, resolved from id if missing)")
    customer_allergens: list[str] = Field(default_factory=list, description="Allergen IDs or names for safety filter")
    customer_diets: list[str] = Field(default_factory=list, description="Diet names for compliance filter (e.g. Vegan)")
    limit: int = Field(5, ge=1, le=20)
    debug: bool = Field(False, description="Include debug_info in response")


class IngredientSubstitutionItem(BaseModel):
    """Single substitute suggestion."""
    ingredient_id: str
    name: str
    reason: str
    source: str = "unknown"
    nutritionComparison: dict[str, Any] | None = None
    allergenSafe: bool = True


class IngredientSubstitutionResponse(BaseModel):
    """Response from POST /substitutions/ingredient."""
    substitutions: list[IngredientSubstitutionItem]
    debug_info: dict[str, Any] | None = None


# ── Product recommendation schemas ──────────────────────────────────────────

class ProductsRequest(BaseModel):
    """Request for POST /recommend/products (grocery list)."""
    ingredient_ids: list[str] = Field(..., description="Ingredient UUIDs to match products for")
    customer_allergens: list[str] = Field(default_factory=list, description="Allergen IDs/names for safety filter")
    quality_preferences: list[str] | None = Field(
        default=None,
        description="Quality toggles e.g. organic, non_gmo, halal, kosher (hard filter)",
    )
    preferred_brands: list[str] | None = Field(
        default=None,
        description="Preferred brand names (soft boost in ranking)",
    )


class ProductMatchItem(BaseModel):
    """Single product match for an ingredient."""
    ingredient_id: str
    product_id: str
    product_name: str
    brand: str = ""
    price: float | None = None
    currency: str = "USD"
    weight_g: int | None = None
    category: str = ""
    image_url: str = ""
    match_reason: str = ""
    preference_matched: bool = False


class ProductsResponse(BaseModel):
    """Response from POST /recommend/products."""
    products: list[ProductMatchItem]


# ── Notification generation (PRD-29) ────────────────────────────────────────

class NotificationGenerateRequest(BaseModel):
    """Request for POST /notifications/generate (PRD-29)."""
    customer_id: str = Field(..., description="B2C customer UUID")
    trigger_type: str = Field(..., description="e.g. missed_breakfast, suggest_breakfast")
    meal_log_summary: dict[str, Any] = Field(default_factory=dict)
    health_profile: dict[str, Any] = Field(default_factory=dict)
    timezone: str = Field("UTC", description="IANA timezone")


class NotificationGenerateResponse(BaseModel):
    """Response from POST /notifications/generate."""
    title: str
    body: str
    action_url: str
    icon: str
    type: str = Field(..., description="meal | nutrition | grocery | budget | family | system")


class AlternativesRequest(BaseModel):
    """Request for POST /recommend/alternatives (scanner)."""
    product_id: str = Field(..., description="Scanned product UUID")
    customer_allergens: list[str] = Field(default_factory=list, description="Allergen IDs/names for safety filter")
    limit: int = Field(5, ge=1, le=20)


class AlternativeItem(BaseModel):
    """Single alternative product."""
    product_id: str
    name: str
    brand: str = ""
    price: float | None = None
    image_url: str = ""
    reason: str = ""
    savings: float | None = None
    allergen_safe: bool = True
    category: str = ""


class AlternativesResponse(BaseModel):
    """Response from POST /recommend/alternatives."""
    alternatives: list[AlternativeItem]


# ── Chatbot schemas ────────────────────────────────────────────────────────

class ChatProcessRequest(BaseModel):
    """Chatbot message from Express (POST /chat/process)."""
    message: str = Field(..., min_length=1, max_length=1000, description="User message")
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
    confidence: float | None = Field(None, description="Intent extraction confidence (0–1), when available")
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
            diets_raw = record["diets"] or []
            diets_clean = [d for d in diets_raw if d and isinstance(d, str)]
            return {
                "display_name":    name,
                "diets":           diets_clean,
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

def _is_uuid(val: str) -> bool:
    """Return True if val looks like a UUID (xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx)."""
    if not val or not isinstance(val, str):
        return False
    return bool(re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", val.lower()))


def _looks_like_element_id(val: str) -> bool:
    """Return True if val looks like Neo4j elementId (e.g. 4:abc123:42)."""
    if not val or not isinstance(val, str):
        return False
    return ":" in val and len(val) > 5 and not _is_uuid(val)


def _lookup_uuid_from_neo4j(
    driver: Driver,
    *,
    element_id: str | None = None,
    label: str = "Recipe",
    title: str | None = None,
    name: str | None = None,
    database: str | None = None,
) -> str | None:
    """
    Lookup PostgreSQL UUID from Neo4j when it is not present in the payload.
    Uses elementId, or label+title/name depending on what is available.
    """
    try:
        with driver.session(database=database) as session:
            if element_id and _looks_like_element_id(element_id):
                rec = session.run(
                    "MATCH (n) WHERE elementId(n) = $elem_id RETURN n.id AS id",
                    elem_id=element_id,
                ).single()
                if rec and rec["id"]:
                    return str(rec["id"])
            if label and (title or name):
                prop = "title" if title else "name"
                val = (title or name or "").strip()
                if not val:
                    return None
                cypher = (
                    f"MATCH (n:{label}) WHERE toLower(n.{prop}) = toLower($val) "
                    "RETURN n.id AS id LIMIT 1"
                )
                rec = session.run(cypher, val=val).single()
                if rec and rec["id"]:
                    return str(rec["id"])
    except Exception as e:
        logger.warning(
            "UUID lookup failed",
            extra={"element_id": element_id, "label": label, "title": title, "name": name, "error": str(e)},
        )
    return None


def _resolve_id(payload: dict[str, Any], key: str) -> str:
    """
    Resolve the best available recipe ID from a fused result payload.
    Returns only what is in payload; does not perform DB lookup.
    Priority: payload.id > payload["r.id"] > nested payload.id (structural) > key fallback.
    """
    uid = (
        payload.get("id")
        or payload.get("r.id")
        or (payload.get("payload") or {}).get("id")
        or (payload.get("payload") or {}).get("r.id")
    )
    if uid:
        return str(uid)
    return key or str(id(payload))


def _resolve_id_with_lookup(
    payload: dict[str, Any],
    key: str,
    item: dict[str, Any],
    driver: Driver,
    *,
    label: str = "Recipe",
    database: str | None = None,
) -> str:
    """
    Resolve recipe ID to PostgreSQL UUID. Never returns title or elementId.
    When UUID is not in payload, performs Neo4j lookup by elementId or title/name.
    """
    uid = _resolve_id(payload, key)
    if _is_uuid(uid):
        return uid
    # Try lookup: use elementId (connected_id or key) or title
    connected_id = item.get("connected_id") or (key if _looks_like_element_id(key) else None)
    title = payload.get("title") or payload.get("r.title") or (payload.get("payload") or {}).get("title")
    name = payload.get("name") or (payload.get("payload") or {}).get("name")
    looked_up = _lookup_uuid_from_neo4j(
        driver,
        element_id=connected_id or (key if _looks_like_element_id(key) else None),
        label=label,
        title=title or (key if not _looks_like_element_id(key) else None),
        name=name,
        database=database,
    )
    if looked_up:
        return looked_up
    # Last resort: return uid (will be logged; frontend should handle invalid UUID)
    logger.warning(
        "Could not resolve UUID for item; node may lack id property",
        extra={"key": key[:80], "title": (title or name or "")[:80]},
    )
    return uid


def _merge_results_with_profile(
    fused: list[dict[str, Any]],
    entities: dict[str, Any],
    profile: dict[str, Any],
    *,
    driver: Driver,
    database: str | None = None,
    label: str = "Recipe",
    limit: int = 20,
) -> list[RecommendationResult]:
    """
    Build RecommendationResult list from raw fused RRF results for personalized endpoints.
    Used by /recommend/feed and /recommend/meal-candidates (bypasses OrchestratorResult).

    Always returns results with PostgreSQL UUID. When UUID is not in payload,
    performs Neo4j lookup by elementId or title. Never skips recipes.
    """
    out: list[RecommendationResult] = []
    for item in fused:
        if len(out) >= limit:
            break
        payload = item.get("payload", {}) or {}
        key = item.get("key", "")
        title = item.get("title") or payload.get("title") or payload.get("name") or key
        rec_id = _resolve_id_with_lookup(payload, key, item, driver, label=label, database=database)

        out.append(
            RecommendationResult(
                id=str(rec_id),
                score=float(item.get("rrf_score", 0.0)),
                reasons=_build_reasons(item, entities, profile),
                metadata={
                    "title": title,
                    "label": item.get("label", ""),
                    "sources": item.get("sources", []),
                    "id_source": "uuid" if _is_uuid(rec_id) else "lookup",
                },
            )
        )
    return out


# ── Helpers ────────────────────────────────────────────────────────────────

def _merge_results(
    orch: OrchestratorResult,
    *,
    driver: Driver,
    database: str | None = None,
    label: str = "Recipe",
    limit: int = 20,
) -> list[RecommendationResult]:
    """
    Merge fused RRF results into a ranked list for API response.
    Uses OrchestratorResult.fused_results (already RRF-fused by apply_rrf()).

    Always returns PostgreSQL UUID. When not in payload, performs Neo4j lookup.
    Never returns title or elementId as id.
    """
    fused = orch.fused_results[:limit]
    out: list[RecommendationResult] = []

    for item in fused:
        payload = item.get("payload", {}) or {}
        key = item.get("key", "")
        title = item.get("title") or payload.get("title") or payload.get("name") or key
        rec_id = _resolve_id_with_lookup(payload, key, item, driver, label=label, database=database)

        out.append(
            RecommendationResult(
                id=str(rec_id),
                score=float(item.get("rrf_score", 0.0)),
                reasons=_build_reasons(item, orch.entities, profile=None),
                metadata={
                    "title": title,
                    "label": item.get("label", ""),
                    "sources": item.get("sources", []),
                    "id_source": "uuid" if _is_uuid(rec_id) else "lookup",
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
        payload: dict[str, Any] = {"status": "ok", "neo4j": "connected"}
        pg_sync = os.getenv("PG_SYNC_LAST_RUN")
        if pg_sync:
            payload["pg_sync_last_run"] = pg_sync
        return payload
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
    identity = (req.customer_id or "anonymous").strip() or "anonymous"
    check_rate_limit(identity)
    start = time.time()
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

    recommendations = _merge_results(
        result,
        driver=_driver,
        database=database,
        label="Recipe",
        limit=req.limit,
    )

    zero_explanation = result.fallback_message if not recommendations else None
    latency_ms = (time.time() - start) * 1000
    logger.info(
        "search_hybrid complete",
        extra={"endpoint": "search_hybrid", "identity": f"{identity[:8]}..." if len(identity) > 8 else identity, "intent": result.intent, "latency_ms": round(latency_ms)},
    )
    return SearchResponse(
        results=recommendations,
        intent=result.intent,
        entities=result.entities,
        retrieval_time_ms=latency_ms,
        zero_results_explanation=zero_explanation,
        confidence=result.confidence,
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
    check_rate_limit((req.customer_id or "").strip() or "anonymous")
    start = time.time()
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

    # ── Hard constraints: allergens/exclude_ingredient, course, calories ───
    fused = apply_hard_constraints(
        fused, entities, "find_recipe", _driver, database=database,
    )

    # ── Post-filter: remove recipes eaten in the last 14 days ─────────────
    recent = {t.lower() for t in profile["recent_recipes"]}
    if recent:
        fused = [f for f in fused if (f.get("title") or "").lower() not in recent]

    recommendations = _merge_results_with_profile(
        fused, entities, profile,
        driver=_driver,
        database=database,
        label="Recipe",
        limit=req.limit,
    )

    zero_explanation = None
    if not recommendations:
        zero_explanation = build_zero_results_message(entities, "find_recipe")
    latency_ms = (time.time() - start) * 1000
    identity = (req.customer_id or "").strip() or "anonymous"
    logger.info("recommend_feed complete", extra={"endpoint": "recommend_feed", "identity": f"{identity[:8]}..." if len(identity) > 8 else identity, "latency_ms": round(latency_ms)})
    return SearchResponse(
        results=recommendations,
        intent="find_recipe",
        entities=entities,
        retrieval_time_ms=latency_ms,
        zero_results_explanation=zero_explanation,
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
    identity = (req.customer_id or "").strip() or "anonymous"
    check_rate_limit(identity)
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

    # ── Hard constraints: allergens/exclude_ingredient, course, calories ───
    fused = apply_hard_constraints(
        fused, entities, "find_recipe", _driver, database=database,
    )

    # ── Post-filter: exclude recently eaten + meal_history + exclude_ids ───
    exclude_ids = {rid.strip().lower() for rid in (req.meal_history or []) + (req.exclude_ids or []) if rid}
    recent_titles = {t.lower() for t in profile["recent_recipes"] or []}

    def _should_exclude(item: dict[str, Any]) -> bool:
        payload = item.get("payload", {}) or {}
        key = item.get("key", "")
        rec_id = _resolve_id_with_lookup(payload, key, item, _driver, label="Recipe", database=database)
        title = (payload.get("title") or payload.get("name") or key or "").lower()
        if rec_id and str(rec_id).lower() in exclude_ids:
            return True
        if title in recent_titles:
            return True
        return False

    fused = [f for f in fused if not _should_exclude(f)]

    # ── Map to MealCandidateItem format ────────────────────────────────────
    recs = _merge_results_with_profile(
        fused, entities, profile,
        driver=_driver,
        database=database,
        label="Recipe",
        limit=req.limit,
    )
    candidates = [
        MealCandidateItem(
            recipe_id=r.id,
            title=r.metadata.get("title", r.id),
            score=r.score,
            reasons=r.reasons,
        )
        for r in recs
    ]

    zero_explanation = None
    if not candidates:
        zero_explanation = build_zero_results_message(entities, "find_recipe")
    latency_ms = (time.time() - start) * 1000
    logger.info("recommend_meal_candidates complete", extra={"endpoint": "recommend_meal_candidates", "identity": f"{identity[:8]}..." if len(identity) > 8 else identity, "latency_ms": round(latency_ms)})
    return MealCandidateResponse(
        candidates=candidates,
        retrieval_time_ms=latency_ms,
        zero_results_explanation=zero_explanation,
    )


@app.post("/substitutions/ingredient", response_model=IngredientSubstitutionResponse, dependencies=[Depends(verify_api_key)])
async def substitutions_ingredient(req: IngredientSubstitutionRequest):
    """
    Get ingredient substitution suggestions.

    Flow: Graph (CAN_SUBSTITUTE) → Semantic retrieval → Allergen filter → Diet filter
    → Nutrition enrichment → LLM fallback if no candidates.
    """
    check_rate_limit("anonymous")
    start = time.time()
    database = os.getenv("NEO4J_DATABASE")
    result = run_ingredient_substitution(
        _driver,
        cfg=_cfg,
        embedder=_embedder,
        ingredient_id=req.ingredient_id,
        ingredient_name=req.ingredient_name,
        customer_allergens=req.customer_allergens or [],
        customer_diets=req.customer_diets or [],
        limit=req.limit,
        database=database,
        debug=req.debug,
    )
    substitutions = [
        IngredientSubstitutionItem(
            ingredient_id=s.get("ingredient_id", ""),
            name=s.get("name", ""),
            reason=s.get("reason", ""),
            source=s.get("source", "unknown"),
            nutritionComparison=s.get("nutritionComparison"),
            allergenSafe=s.get("allergenSafe", True),
        )
        for s in result.get("substitutions", [])
    ]
    latency_ms = (time.time() - start) * 1000
    logger.info("substitutions_ingredient complete", extra={"endpoint": "substitutions_ingredient", "identity": "anonymous", "latency_ms": round(latency_ms)})
    return IngredientSubstitutionResponse(
        substitutions=substitutions,
        debug_info=result.get("debug_info") if req.debug else None,
    )


@app.post("/recommend/products", response_model=ProductsResponse, dependencies=[Depends(verify_api_key)])
async def recommend_products(req: ProductsRequest):
    """
    Match products to ingredients for grocery list (allergen-safe).
    Supports quality_preferences (certification hard filter) and preferred_brands (soft boost).
    Returns empty when Product nodes or CONTAINS_INGREDIENT not available.
    """
    check_rate_limit("anonymous")
    start = time.time()
    database = os.getenv("NEO4J_DATABASE")
    result = run_recommend_products(
        _driver,
        ingredient_ids=req.ingredient_ids,
        customer_allergens=req.customer_allergens or [],
        quality_preferences=req.quality_preferences,
        preferred_brands=req.preferred_brands,
        database=database,
    )
    products = [
        ProductMatchItem(
            ingredient_id=p.get("ingredient_id", ""),
            product_id=p.get("product_id", ""),
            product_name=p.get("product_name", ""),
            brand=p.get("brand", ""),
            price=p.get("price"),
            currency=p.get("currency", "USD"),
            weight_g=p.get("weight_g"),
            category=p.get("category", ""),
            image_url=p.get("image_url", ""),
            match_reason=p.get("match_reason", ""),
            preference_matched=p.get("preference_matched", True),
        )
        for p in result.get("products", [])
    ]
    logger.info("recommend_products complete", extra={"endpoint": "recommend_products", "identity": "anonymous", "latency_ms": round((time.time() - start) * 1000)})
    return ProductsResponse(products=products)


@app.post("/recommend/alternatives", response_model=AlternativesResponse, dependencies=[Depends(verify_api_key)])
async def recommend_alternatives(req: AlternativesRequest):
    """
    Find alternative products for a scanned product (allergen-safe, cheaper).
    Uses CAN_SUBSTITUTE or same-category fallback.
    Returns empty when Product data not available.
    """
    check_rate_limit("anonymous")
    start = time.time()
    database = os.getenv("NEO4J_DATABASE")
    result = run_recommend_alternatives(
        _driver,
        product_id=req.product_id,
        customer_allergens=req.customer_allergens or [],
        limit=req.limit,
        database=database,
    )
    alternatives = [
        AlternativeItem(
            product_id=a.get("product_id", ""),
            name=a.get("name", ""),
            brand=a.get("brand", ""),
            price=a.get("price"),
            image_url=a.get("image_url", ""),
            reason=a.get("reason", ""),
            savings=a.get("savings"),
            allergen_safe=a.get("allergen_safe", True),
            category=a.get("category", ""),
        )
        for a in result.get("alternatives", [])
    ]
    logger.info("recommend_alternatives complete", extra={"endpoint": "recommend_alternatives", "identity": "anonymous", "latency_ms": round((time.time() - start) * 1000)})
    return AlternativesResponse(alternatives=alternatives)


@app.post("/notifications/generate", response_model=NotificationGenerateResponse, dependencies=[Depends(verify_api_key)])
async def notifications_generate(req: NotificationGenerateRequest):
    """
    Generate notification copy for PRD-29 auto-notifications.
    Template-based (no LLM). Backend passes trigger_type, meal_log_summary, health_profile.
    For suggest_breakfast/suggest_lunch, meal_log_summary may include suggested_recipe: { id, title }.
    """
    check_rate_limit("anonymous")
    start = time.time()
    result = generate_notification(
        trigger_type=req.trigger_type,
        meal_log_summary=req.meal_log_summary,
        health_profile=req.health_profile,
        timezone=req.timezone,
    )
    logger.info(
        "notifications_generate complete",
        extra={
            "endpoint": "notifications_generate",
            "trigger_type": req.trigger_type,
            "latency_ms": round((time.time() - start) * 1000),
        },
    )
    return NotificationGenerateResponse(**result)


@app.post("/chat/process", response_model=ChatProcessResponse, dependencies=[Depends(verify_api_key)])
async def chat_process(req: ChatProcessRequest):
    """
    Chatbot message handler: NLU → intent routing → graph queries → LLM response.

    Called by Express POST /api/v1/chat. Session is created/reused for multi-turn context.
    NLU and response generation will be wired in Phase 3; currently returns a stub response.
    """
    identity = (req.customer_id or req.session_id or "anonymous").strip() or "anonymous"
    check_rate_limit(identity)
    start = time.time()
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

    # Expand follow-up queries using conversation context (e.g. "Can I use soy then?" -> full query)
    history_pairs = [
        (m.role, m.content)
        for m in session.history[:-1]
        if m.role in ("user", "assistant")
    ]
    effective_msg = expand_query_with_context(msg, history_pairs) if history_pairs else msg

    # NLU: intent + entities (rules first, LLM fallback)
    nlu_result = extract_hybrid(effective_msg)

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
                user_query=effective_msg,
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
                effective_msg,
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
                recs = _merge_results(
                    orch_result,
                    driver=_driver,
                    database=database,
                    label="Recipe",
                    limit=5,
                )
                recipes_out = [
                    ChatRecipeItem(
                        id=r.id,
                        title=r.metadata.get("title", r.id),
                        score=r.score,
                    )
                    for r in recs
                ]

            latency_ms = (time.time() - start) * 1000
            logger.info("chat_process complete", extra={"endpoint": "chat_process", "identity": f"{identity[:8]}..." if len(identity) > 8 else identity, "intent": nlu_result.intent, "latency_ms": round(latency_ms)})
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
                confidence=orch_result.confidence,
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
