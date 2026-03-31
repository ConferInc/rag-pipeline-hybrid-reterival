"""
FastAPI application wrapping the RAG pipeline for B2C frontend integration.

Architecture:
  Express Backend --HTTP/REST--> This API --> Neo4j + LLM
  (Handles user auth)            (Handles retrieval + generation)

Dependency direction: api → rag_pipeline (one-way). Core pipeline is untouched.
"""

from __future__ import annotations

import hmac
import logging
import os
import random
import re
import time
from itertools import combinations
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
from rag_pipeline.augmentation.response_sanitizer import sanitize_response
from rag_pipeline.config import load_embedding_config
from rag_pipeline.embeddings.caching_embedder import CachingQueryEmbedder
from rag_pipeline.embeddings.openai_embedder import OpenAIQueryEmbedder
from rag_pipeline.neo4j_client import create_neo4j_driver, neo4j_settings_from_env
from rag_pipeline.profile import aggregate_profile, get_household_id_for_customer, get_household_type, resolve_profile_for_recommendation
from rag_pipeline.nlu.intents import CHATBOT_DATA_INTENTS, DATA_INTENTS_NEEDING_RETRIEVAL
from rag_pipeline.orchestrator.constraint_filter import (
    apply_hard_constraints,
    apply_usda_food_group_bonus,
    build_zero_results_message,
    contextual_rerank,
)
from rag_pipeline.orchestrator.cypher_runner import run_cypher_retrieval
from rag_pipeline.orchestrator.profile_enrichment import merge_profile_into_entities
from rag_pipeline.orchestrator.orchestrator import orchestrate, OrchestratorResult
from rag_pipeline.orchestrator.food_group_audit import (
    audit_candidate_set,
    build_audit_warnings,
)
from rag_pipeline.orchestrator.usda_guidelines import (
    guidelines_to_jsonable,
    infer_food_groups_for_ingredients,
    load_usda_guidelines,
    load_usda_soft_guidelines,
)
from rag_pipeline.retrieval.service import retrieve_semantic, SemanticRetrievalRequest
from rag_pipeline.retrieval.similar_constraint import retrieve_recipes_from_similar_constraint_users
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
_response_validation_cfg: dict[str, Any] = {}
_usda_guidelines: dict[str, Any] | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize Neo4j driver + embedder on startup, close on shutdown."""
    global _driver, _cfg, _embedder, _response_validation_cfg
    global _usda_guidelines

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
        _response_validation_cfg = (raw_cfg or {}).get("response_validation", {}) or {}
    except Exception:
        cache_cfg = {}
        _response_validation_cfg = {}
    if cache_cfg.get("enabled", False):
        _embedder = CachingQueryEmbedder(
            base_embedder,
            max_size=cache_cfg.get("max_size", 500),
            key_normalize=cache_cfg.get("key_normalize", "strip_lower"),
        )
    else:
        _embedder = base_embedder

    # Phase A/PRD-34: load USDA hard + soft guideline config (defaults + cache)
    # and attach into orchestrator entities for prompt/scoring/audit contracts.
    try:
        hard_guidelines = guidelines_to_jsonable(load_usda_guidelines())
        hard_guidelines["soft_guidelines"] = load_usda_soft_guidelines()
        _usda_guidelines = hard_guidelines
    except Exception:
        _usda_guidelines = None

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
    if not expected or not hmac.compare_digest(x_api_key, expected):
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
    household_id: str | None = Field(None, description="Household UUID for family-scoped profile resolution")
    scope: str | None = Field(None, description="Profile scope: individual | couple | family (default inferred from household_type)")
    household_type: str | None = Field(None, description="Household type: individual | couple | family (overrides Neo4j lookup)")
    member_id: str | None = Field(None, description="Active household member UUID (when different from customer_id)")
    total_members: int | None = Field(None, description="Number of household members")
    member_profile: dict[str, Any] | None = Field(
        None,
        description="Pre-built profile: {diets: [], allergens: [], health_conditions: [], household_type?, ...}. Primary over Neo4j when provided.",
    )
    context: dict[str, Any] | None = Field(None, description="RecommendationContext from B2C")


class FeedRequest(BaseModel):
    """Personalized recipe feed — no user query needed, driven by customer profile."""
    customer_id: str = Field(..., description="B2C customer UUID (required for personalization)")
    meal_type: str | None = Field(None, description="Optional meal type hint: breakfast/lunch/dinner/snack")
    limit: int = Field(50, ge=1, le=50)
    household_id: str | None = Field(None, description="Household UUID for family-scoped profile resolution")
    scope: str | None = Field(None, description="Profile scope: individual | couple | family (default inferred from household_type)")
    household_type: str | None = Field(None, description="Household type: individual | couple | family (overrides Neo4j lookup)")
    member_id: str | None = Field(None, description="Active household member UUID (when different from customer_id)")
    total_members: int | None = Field(None, description="Number of household members")
    preferences: dict[str, Any] | None = Field(
        None,
        description="B2C preferences: {dietIds, allergenIds, conditionIds, dislikes} (primary over Neo4j when provided)",
    )
    member_profile: dict[str, Any] | None = Field(
        None,
        description="Pre-built profile: {diets, allergens, health_conditions, health_goal, household_type?, ...}. Primary over Neo4j when provided.",
    )
    context: dict[str, Any] | None = Field(None, description="RecommendationContext from B2C")


class MealCandidateRequest(BaseModel):
    """
    Pre-scored recipe candidates for meal plan generation.
    Supports household scope: individual (self), couple (both primary adults), family (all members).
    B2C can send members[] (per-member profiles) or member_profile/household_id; Neo4j used as fallback.
    """
    customer_id: str = Field(..., description="B2C customer UUID")
    meal_history: list[str] = Field(default_factory=list, description="Recipe IDs to exclude (e.g. from PostgreSQL meal_logs)")
    meal_type: str | None = Field(None, description="Optional: breakfast/lunch/dinner/snack")
    exclude_ids: list[str] = Field(default_factory=list, description="Additional recipe IDs to exclude (e.g. for swap)")
    limit: int = Field(50, ge=1, le=100)
    household_id: str | None = Field(None, description="Household UUID for family-scoped profile resolution")
    scope: str | None = Field(None, description="Profile scope: individual | couple | family (default inferred from household_type)")
    household_type: str | None = Field(None, description="Household type: individual | couple | family (overrides Neo4j lookup)")
    member_id: str | None = Field(None, description="Active household member UUID")
    total_members: int | None = Field(None, description="Number of household members")
    members: list[dict[str, Any]] | None = Field(
        None,
        description="Per-member profiles [{allergenIds, dietIds, conditionIds, ...}]. Primary over Neo4j when provided.",
    )
    date_range: dict[str, Any] | None = Field(
        None,
        description="Meal plan date range: {start: YYYY-MM-DD, end: YYYY-MM-DD}",
    )
    meals_per_day: int | None = Field(None, description="Target meals per day (e.g. 3)")
    member_profile: dict[str, Any] | None = Field(
        None,
        description="Pre-built aggregated profile. Primary over Neo4j when provided.",
    )
    context: dict[str, Any] | None = Field(
        None,
        description="B2C RecommendationContext: timezone, season, mealTimeSlot, macro targets, recentMealIds, cuisinePreferences",
    )


class MealCandidateItem(BaseModel):
    """Single recipe candidate with score and reasons."""
    recipe_id: str
    title: str
    score: float
    reasons: list[str] = Field(default_factory=list)
    # Phase A contract: inferred USDA food groups for later meal-plan audits.
    food_groups: list[str] = Field(default_factory=list)
    calories: float | None = Field(None, description="Recipe calories used for calorie-fit planning")


class MealCandidateResponse(BaseModel):
    candidates: list[MealCandidateItem]
    retrieval_time_ms: float
    zero_results_explanation: str | None = Field(None, description="Explanation when no candidates satisfy constraints")
    guideline_compliance: str | None = Field(
        None,
        description='USDA guideline compliance summary: "adequate" | "partial"',
    )
    audit_warnings: list[str] = Field(default_factory=list)
    missing_groups: list[str] = Field(default_factory=list)
    food_group_audit: list[dict[str, Any]] = Field(default_factory=list)
    strict_mode_insufficiency: dict[str, Any] | None = Field(
        None,
        description="Structured insufficiency explanation when USDA strict mode is enabled and coverage is infeasible.",
    )
    daily_calorie_target: float | None = Field(None, description="Profile daily calorie target used for plan calibration")
    selected_total_calories: float | None = Field(None, description="Sum of calories for selected meals in this response")
    calorie_delta: float | None = Field(None, description="selected_total_calories - daily_calorie_target")
    calorie_tolerance: float | None = Field(None, description="Allowed absolute deviation for calorie compliance")
    calorie_compliance: str | None = Field(None, description='"adequate" | "partial" based on calorie_delta and tolerance')
    calorie_phase: str = Field("phase_1_2_3", description="Calorie calibration implementation phase marker")


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
    household_type: str | None = Field(None, description="Household type: individual | couple | family")
    total_members: int | None = Field(None, description="Number of household members")
    household_budget: float | None = Field(None, description="Household budget (e.g. USD amount) for product filtering")
    ingredient_names: dict[str, str] | None = Field(
        None,
        description="Map of ingredient_id -> ingredient_name for name-based matching fallback when IDs differ across systems",
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
    household_id: str | None = Field(None, description="Household UUID for family-scoped profile resolution")
    household_type: str | None = Field(None, description="Household type: individual | couple | family (overrides Neo4j lookup)")
    member_id: str | None = Field(None, description="Active household member UUID (for profile resolution)")
    total_members: int | None = Field(None, description="Number of household members")
    member_profile: dict[str, Any] | None = Field(
        None,
        description="Pre-built profile. Primary over Neo4j when provided.",
    )
    context: dict[str, Any] | None = Field(None, description="RecommendationContext from B2C")


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


def _preferences_to_profile(preferences: dict[str, Any]) -> dict[str, Any]:
    """
    Convert B2C preferences dict to profile shape.
    Supports: dietIds/diets, allergenIds/allergens, conditionIds/health_conditions.
    B2C may send IDs or names; we use names when present, else IDs (Cypher may match both).
    """
    def _list(v: Any) -> list[str]:
        if not v:
            return []
        if isinstance(v, list):
            return [str(x).strip() for x in v if x and isinstance(x, (str, int))]
        return []

    diets = _list(preferences.get("diets") or preferences.get("dietNames") or preferences.get("dietIds"))
    allergens = _list(preferences.get("allergens") or preferences.get("allergenNames") or preferences.get("allergenIds"))
    conditions = _list(preferences.get("health_conditions") or preferences.get("conditionNames") or preferences.get("conditionIds"))
    dislikes = _list(preferences.get("dislikes"))
    # dislikes can be added to exclude_ingredient; for now we merge into allergens-like exclusion
    exclude = list(set(allergens) | set(dislikes)) if dislikes else allergens
    ht_raw = preferences.get("household_type")
    ht = None
    if ht_raw and isinstance(ht_raw, str):
        h = ht_raw.strip().lower()
        if h in ("individual", "couple", "family"):
            ht = h
    return {
        "display_name": None,
        "diets": diets,
        "allergens": exclude,
        "health_conditions": conditions,
        "health_goal": preferences.get("health_goal") if isinstance(preferences.get("health_goal"), str) else None,
        "activity_level": preferences.get("activity_level") if isinstance(preferences.get("activity_level"), str) else None,
        "recent_recipes": [],
        "household_type": ht,
    }


def _merge_b2c_with_neo4j(b2c: dict[str, Any], neo4j: dict[str, Any]) -> dict[str, Any]:
    """
    Merge B2C profile with Neo4j profile. B2C takes precedence when non-empty; Neo4j fills gaps.
    """
    result: dict[str, Any] = {}
    for k in ("display_name", "diets", "allergens", "health_conditions", "health_goal", "activity_level", "recent_recipes", "household_type"):
        b_val = b2c.get(k)
        n_val = neo4j.get(k)
        if k in ("diets", "allergens", "health_conditions", "recent_recipes"):
            if b_val and isinstance(b_val, list) and len(b_val) > 0:
                result[k] = list(b_val)
            elif n_val and isinstance(n_val, list):
                result[k] = list(n_val)
            else:
                result[k] = []
        elif k == "household_type":
            result[k] = b_val if (b_val and isinstance(b_val, str)) else (n_val if (n_val and isinstance(n_val, str)) else None)
        else:
            result[k] = b_val if (b_val is not None and b_val != "") else n_val
    return result


def _members_to_profiles(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert B2C members[] to list of profile-shaped dicts for aggregate_profile."""
    profiles: list[dict[str, Any]] = []
    for m in members:
        if not isinstance(m, dict):
            continue
        prof = {
            "display_name": m.get("display_name"),
            "diets": list(m.get("diets") or m.get("dietIds") or []),
            "allergens": list(m.get("allergens") or m.get("allergenIds") or []),
            "health_conditions": list(m.get("health_conditions") or m.get("conditionIds") or []),
            "health_goal": m.get("health_goal"),
            "activity_level": m.get("activity_level"),
            "recent_recipes": list(m.get("recent_recipes") or []),
        }
        profiles.append(prof)
    return profiles


def _member_profile_to_profile(member_profile: dict[str, Any]) -> dict[str, Any]:
    """Convert member_profile dict from request to fetch_customer_profile shape."""
    ht_raw = member_profile.get("household_type")
    ht = None
    if ht_raw and isinstance(ht_raw, str):
        h = ht_raw.strip().lower()
        if h in ("individual", "couple", "family"):
            ht = h
    return {
        "display_name": member_profile.get("display_name"),
        "diets": list(member_profile.get("diets") or []),
        "allergens": list(member_profile.get("allergens") or []),
        "health_conditions": list(member_profile.get("health_conditions") or []),
        "health_goal": member_profile.get("health_goal"),
        "activity_level": member_profile.get("activity_level"),
        "recent_recipes": list(member_profile.get("recent_recipes") or []),
        "household_type": ht,
    }


def _infer_default_scope(
    driver: Driver,
    customer_id: str | None,
    household_id: str | None,
    database: str | None,
    household_type_override: str | None = None,
) -> str:
    """
    Infer default scope from household_type when client did not provide scope.
    Returns "individual" | "couple" | "family".
    """
    if household_type_override and isinstance(household_type_override, str):
        ht = household_type_override.strip().lower()
        if ht in ("individual", "couple", "family"):
            return ht
    hh_id = household_id
    if not hh_id and customer_id:
        hh_id = get_household_id_for_customer(driver, customer_id, database)
    if not hh_id:
        return "individual"
    ht = get_household_type(driver, hh_id, database)
    return {"individual": "individual", "couple": "couple", "family": "family"}.get(ht or "", "individual")


def _is_aggregated_profile(
    *,
    scope: str | None = None,
    family_scope: str | None = None,
    target_member_role: str | None = None,
) -> bool:
    """True when profile is aggregated (family/couple) — use similar-constraint retrieval."""
    if scope:
        s = (scope or "").strip().lower()
        if s in ("family", "couple"):
            return True
    if (family_scope or "").strip().lower() == "family":
        return True
    if (target_member_role or "").strip():
        return True
    return False


def _resolve_profile(
    driver: Driver,
    customer_id: str,
    database: str | None,
    *,
    household_id: str | None = None,
    scope: str | None = None,
    family_scope: str | None = None,
    target_member_role: str | None = None,
    member_profile: dict[str, Any] | None = None,
    household_type_override: str | None = None,
    member_id: str | None = None,
) -> dict[str, Any]:
    """
    Resolve profile for recommendations. Uses member_profile when provided;
    else resolve_profile_for_recommendation with scope / member_id / NLU entities.
    When scope is missing, infers from household_type (request or Neo4j).
    member_id overrides which household member's profile to use (B2C override).
    """
    if member_profile and isinstance(member_profile, dict):
        return _member_profile_to_profile(member_profile)
    effective_scope = (scope or "").strip() or None
    if not effective_scope:
        effective_scope = _infer_default_scope(
            driver, customer_id, household_id, database,
            household_type_override=household_type_override,
        )
    fs = family_scope
    tm = target_member_role
    if effective_scope:
        s = effective_scope.strip().lower()
        if s == "family":
            fs = "family"
        elif s == "couple":
            tm = "primary_adult"
    return resolve_profile_for_recommendation(
        driver,
        customer_id,
        household_id=household_id,
        member_id=member_id,
        family_scope=fs,
        target_member_role=tm,
        database=database,
    )


_GOAL_WORDS: dict[str, str] = {
    "weight_loss":   "low calorie",
    "muscle_gain":   "high protein",
    "heart_health":  "low fat",
    "energy":        "energizing",
    "general_health": "healthy",
}


def build_feed_query_text(
    profile: dict[str, Any],
    meal_type: str | None = None,
    entities: dict[str, Any] | None = None,
) -> str:
    """
    Build a short text string for the semantic embedder from a customer profile.
    This text is NOT passed to the LLM — it is only embedded for vector similarity search.

    When entities is provided (PRD-33), includes cuisine, season, region, course/meal_time.

    Examples:
      diets=["Vegan"], goal="weight_loss"  → "Vegan low calorie recipes"
      diets=[],        goal="muscle_gain"  → "high protein recipes"
      meal_type="breakfast"                → "Vegan low calorie breakfast recipes"
    """
    parts: list[str] = list(profile.get("diets") or [])
    if entities:
        cuisines = entities.get("cuisine_preference", [])
        if isinstance(cuisines, list) and cuisines:
            for c in cuisines[:3]:
                if c and str(c).strip():
                    parts.append(str(c).strip())
        elif cuisines and isinstance(cuisines, str):
            parts.append(str(cuisines).strip())
        season = entities.get("season")
        if season and str(season).strip():
            parts.append(str(season).strip())
        region = entities.get("region")
        if region and str(region).strip():
            parts.append(str(region).strip())
    goal_text = _GOAL_WORDS.get(profile.get("health_goal") or "", "healthy")
    parts.append(goal_text)
    if meal_type:
        parts.append(meal_type)
    elif entities:
        course_val = entities.get("course") or entities.get("meal_time")
        if course_val and str(course_val).strip():
            parts.append(str(course_val).strip())
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


def _resolve_profile_ids_to_names(
    driver: Driver,
    profile: dict[str, Any],
    database: str | None = None,
) -> dict[str, Any]:
    """
    Resolve diet and allergen IDs (UUIDs) to names in Neo4j so Cypher and semantic
    retrieval get meaningful values. Non-UUID entries (e.g. names) are kept as-is.
    """
    result = dict(profile)
    diets_in = list(profile.get("diets") or [])
    allergens_in = list(profile.get("allergens") or [])

    diet_ids = [x for x in diets_in if _is_uuid(str(x))]
    diet_names_keep = [x for x in diets_in if not _is_uuid(str(x))]
    allergen_ids = [x for x in allergens_in if _is_uuid(str(x))]
    allergen_names_keep = [x for x in allergens_in if not _is_uuid(str(x))]

    diet_names_resolved: list[str] = []
    if diet_ids:
        cypher_d = """
        UNWIND $ids AS id
        MATCH (dp:Dietary_Preferences)
        WHERE dp.id = id OR toString(elementId(dp)) = id
        RETURN dp.name AS name
        """
        try:
            with driver.session(database=database) as session:
                recs = session.run(cypher_d, ids=diet_ids)
                for rec in recs:
                    n = rec.get("name")
                    if n and isinstance(n, str) and n.strip():
                        diet_names_resolved.append(n.strip())
        except Exception as e:
            logger.warning("_resolve_profile_ids_to_names diets lookup failed: %s", e)

    allergen_names_resolved: list[str] = []
    if allergen_ids:
        cypher_a = """
        UNWIND $ids AS id
        MATCH (a:Allergens)
        WHERE a.id = id OR toString(elementId(a)) = id
        RETURN a.name AS name
        """
        try:
            with driver.session(database=database) as session:
                recs = session.run(cypher_a, ids=allergen_ids)
                for rec in recs:
                    n = rec.get("name")
                    if n and isinstance(n, str) and n.strip():
                        allergen_names_resolved.append(n.strip())
        except Exception as e:
            logger.warning("_resolve_profile_ids_to_names allergens lookup failed: %s", e)

    result["diets"] = diet_names_keep + diet_names_resolved
    result["allergens"] = allergen_names_keep + allergen_names_resolved
    return result


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
    Priority: payload.id > nested payload.id (temporary migration fallback) > key fallback.
    """
    uid = (
        payload.get("id")
        or (payload.get("payload") or {}).get("id")
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
    title = payload.get("title") or (payload.get("payload") or {}).get("title")
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


def _fetch_recipe_ingredient_names(
    driver: Driver,
    recipe_ids: list[str],
    *,
    database: str | None = None,
) -> dict[str, list[str]]:
    """
    Batch fetch ingredient names for a list of Recipe UUIDs.

    Phase A: recipes currently don't include `food_groups` in retrieval payloads,
    so we infer buckets using Recipe -> USES_INGREDIENT -> Ingredient.
    """
    if not recipe_ids:
        return {}

    cypher = """
    UNWIND $recipe_ids AS rid
    MATCH (r:Recipe {id: rid})-[:USES_INGREDIENT]->(i:Ingredient)
    RETURN rid AS recipe_id,
           collect(DISTINCT coalesce(i.name, i.title, i.display_name)) AS ingredient_names
    """
    try:
        with driver.session(database=database) as session:
            rows = session.run(cypher, recipe_ids=recipe_ids)
            out: dict[str, list[str]] = {rid: [] for rid in recipe_ids}
            for row in rows:
                rid = str(row.get("recipe_id"))
                ing_names_raw = row.get("ingredient_names") or []
                out[rid] = [str(x).strip() for x in ing_names_raw if x and str(x).strip()]
            return out
    except Exception as e:
        logger.warning(
            "Failed to fetch recipe ingredient names for USDA inference: %s",
            e,
            extra={"component": "usda_food_groups"},
        )
        return {rid: [] for rid in recipe_ids}


def _is_kcal_unit(unit: Any) -> bool:
    """Return True when unit represents kcal/calories variants."""
    if unit is None:
        return False
    text = str(unit).strip().lower()
    if not text:
        return False
    normalized = re.sub(r"[^a-z]", "", text)
    return normalized in {
        "kcal",
        "kcals",
        "kilocalorie",
        "kilocalories",
        "calorie",
        "calories",
        "cal",
    }


def _fetch_recipe_calories_map(
    driver: Driver,
    recipe_ids: list[str],
    *,
    database: str | None = None,
    request_cache: dict[str, float | None] | None = None,
) -> dict[str, float | None]:
    """
    Batch fetch calories from nutrition graph path:
    Recipe -> HAS_NUTRITION -> NutritionValue -> OF_NUTRIENT -> NutrientDefinition.
    """
    if not recipe_ids:
        return {}
    if os.getenv("ENABLE_GRAPH_CALORIE_RESOLVER", "1").strip() != "1":
        return {rid: None for rid in recipe_ids}

    cache = request_cache if request_cache is not None else {}
    missing_for_query = [rid for rid in recipe_ids if rid not in cache]
    if not missing_for_query:
        return {rid: cache.get(rid) for rid in recipe_ids}

    aliases = [
        "Energy",
        "Calories",
        "Calories/Energy",
        "Energy (kcal)",
        "Energy, calories",
    ]
    alias_rank = {
        "energy": 5,
        "calories": 4,
        "calories/energy": 3,
        "energy (kcal)": 2,
        "energy, calories": 1,
    }
    cypher = """
    UNWIND $recipe_ids AS rid
    OPTIONAL MATCH (r:Recipe {id: rid})-[:HAS_NUTRITION]->(nv:NutritionValue)-[:OF_NUTRIENT]->(nd:NutrientDefinition)
    WHERE toLower(coalesce(nd.name, "")) IN $alias_names_lc
    RETURN rid AS recipe_id,
           nd.name AS nutrient_name,
           nv.amount AS amount,
           coalesce(nv.unit, nv.unit_name, nd.unit, "") AS unit
    """
    out: dict[str, float | None] = {rid: cache.get(rid) for rid in recipe_ids}
    try:
        lookup_start = time.perf_counter()
        timeout_s = float(os.getenv("CALORIE_LOOKUP_TIMEOUT_S", "1.5"))
        with driver.session(database=database) as session:
            with session.begin_transaction(timeout=timeout_s) as tx:
                rows = list(
                    tx.run(
                        cypher,
                        recipe_ids=missing_for_query,
                        alias_names_lc=[a.lower() for a in aliases],
                    )
                )
            best_per_recipe: dict[str, tuple[tuple[int, int, int], float]] = {}
            for idx, row in enumerate(rows):
                rid = str(row.get("recipe_id"))
                amount = _safe_float(row.get("amount"))
                if amount is None:
                    continue
                nutrient_name = str(row.get("nutrient_name") or "").strip().lower()
                unit = row.get("unit")
                is_kcal = 1 if _is_kcal_unit(unit) else 0
                # Deterministic selection:
                # 1) kcal unit, 2) nutrient alias confidence, 3) first valid row.
                selection_key = (is_kcal, alias_rank.get(nutrient_name, 0), -idx)
                current = best_per_recipe.get(rid)
                if current is None or selection_key > current[0]:
                    best_per_recipe[rid] = (selection_key, amount)

            for rid, (_, cal) in best_per_recipe.items():
                cache[rid] = float(cal)
                out[rid] = float(cal)

            for rid in missing_for_query:
                if rid not in cache:
                    cache[rid] = None
                out[rid] = cache.get(rid)

            resolved = sum(1 for v in out.values() if v is not None)
            missing = len(out) - resolved
            lookup_ms = (time.perf_counter() - lookup_start) * 1000.0
            if resolved:
                logger.info(
                    "Graph calorie lookup resolved",
                    extra={
                        "component": "calorie_graph_lookup",
                        "counter": "calorie_graph_lookup_success_count",
                        "value": resolved,
                        "latency_ms": round(lookup_ms, 2),
                    },
                )
            if missing:
                logger.warning(
                    "Graph calorie lookup missing values",
                    extra={
                        "component": "calorie_graph_lookup",
                        "counter": "calorie_graph_lookup_missing_count",
                        "value": missing,
                        "latency_ms": round(lookup_ms, 2),
                    },
                )
            if missing and random.random() < 0.1:
                missing_ids = [rid for rid, val in out.items() if val is None][:10]
                logger.info(
                    "Graph calorie lookup sampled missing nutrient mappings",
                    extra={
                        "component": "calorie_graph_lookup",
                        "sample_missing_recipe_ids": missing_ids,
                        "sample_size": len(missing_ids),
                    },
                )
            return out
    except Exception as e:
        logger.warning(
            "Failed to fetch calories from nutrition graph: %s",
            e,
            extra={
                "component": "calorie_graph_lookup",
                "counter": "calorie_graph_lookup_missing_count",
                "value": len(missing_for_query),
            },
        )
        return out


def _food_group_coverage_and_hint(food_groups: list[str]) -> tuple[float, str | None]:
    """
    Compute normalized food-group coverage and an optional USDA balance hint.

    Coverage is the fraction of canonical USDA groups present in the recipe.
    Hint is only returned for low-diversity results and remains optional/non-breaking.
    """
    canonical = {"protein", "dairy", "vegetables", "fruits", "whole_grains"}
    present = {
        str(g).strip().lower()
        for g in (food_groups or [])
        if isinstance(g, str) and str(g).strip().lower() in canonical
    }
    coverage = len(present) / 5.0
    if coverage >= 0.6:
        return coverage, None

    missing_priority = ["protein", "vegetables", "whole_grains", "fruits", "dairy"]
    missing = [g for g in missing_priority if g not in present]
    if not missing:
        return coverage, None
    top_missing = ", ".join(missing[:3])
    return coverage, f"Low food-group diversity; consider adding {top_missing}."


def _safe_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


_CAL_LIMIT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:under|below|less than|at most|upto|up to)\s*(\d{2,5}(?:\.\d+)?)\s*(?:kcal|calories?|cal)\b", re.I),
    re.compile(r"\b(\d{2,5}(?:\.\d+)?)\s*(?:kcal|calories?|cal)\s*(?:or less|or below|max(?:imum)?)\b", re.I),
)


def _inject_calorie_limit_entity(query_text: str, entities: dict[str, Any]) -> dict[str, Any]:
    """
    Deterministically map calorie phrases to entities.cal_upper_limit.
    Preserves existing cal_upper_limit when already set.
    """
    if not isinstance(entities, dict):
        return entities
    existing = entities.get("cal_upper_limit")
    if _safe_float(existing) is not None:
        return entities
    text = str(query_text or "")
    for pattern in _CAL_LIMIT_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        parsed = _safe_float(m.group(1))
        if parsed is None:
            continue
        entities["cal_upper_limit"] = int(parsed) if float(parsed).is_integer() else float(parsed)
        break
    return entities


def _inject_graph_calories_into_fused(
    fused: list[dict[str, Any]],
    *,
    driver: Driver,
    database: str | None = None,
    label: str = "Recipe",
    calorie_cache: dict[str, float | None] | None = None,
) -> list[dict[str, Any]]:
    """
    Enrich fused payload.calories from graph resolver, preserving payload fallback.
    This helps downstream rerankers that read calories from payload.
    """
    if not fused:
        return fused

    resolved_items: list[dict[str, Any]] = []
    recipe_ids: list[str] = []
    for item in fused:
        payload = item.get("payload", {}) or {}
        key = item.get("key", "")
        rec_id = _resolve_id_with_lookup(payload, key, item, driver, label=label, database=database)
        item_copy = dict(item)
        item_copy["_resolved_recipe_id"] = rec_id
        resolved_items.append(item_copy)
        if _is_uuid(str(rec_id)):
            recipe_ids.append(str(rec_id))

    recipe_ids = list(dict.fromkeys(recipe_ids))
    graph_calories_map = _fetch_recipe_calories_map(
        driver,
        recipe_ids,
        database=database,
        request_cache=calorie_cache,
    )

    out: list[dict[str, Any]] = []
    for item in resolved_items:
        payload = item.get("payload", {}) or {}
        payload_copy = dict(payload)
        rid = str(item.get("_resolved_recipe_id") or "")
        graph_cal = graph_calories_map.get(rid)
        if graph_cal is not None:
            payload_copy["calories"] = graph_cal
        item_copy = dict(item)
        item_copy["payload"] = payload_copy
        item_copy.pop("_resolved_recipe_id", None)
        out.append(item_copy)
    return out


def _apply_calorie_fit_rerank(
    fused: list[dict[str, Any]],
    *,
    calorie_target: float | None,
    meals_per_day: int,
) -> list[dict[str, Any]]:
    """
    Phase 2: soft rerank candidates toward per-meal calorie target.
    """
    if not fused or calorie_target is None or calorie_target <= 0:
        return fused
    meals = max(1, meals_per_day)
    target_per_meal = calorie_target / float(meals)
    # Keep a practical tolerance band for smooth penalties.
    band = max(120.0, target_per_meal * 0.25)

    scored: list[dict[str, Any]] = []
    for item in fused:
        item_copy = dict(item)
        payload = item_copy.get("payload") or {}
        base = float(item_copy.get("rrf_score", item_copy.get("score", 0.0)))
        cal = _safe_float(payload.get("calories"))
        if cal is None:
            multiplier = 1.0
        else:
            # Linear fit in [0.7, 1.2] where near-target recipes are boosted.
            deviation = abs(cal - target_per_meal)
            closeness = max(0.0, 1.0 - (deviation / band))
            multiplier = 0.7 + (0.5 * closeness)
        adjusted = base * multiplier
        item_copy["score"] = adjusted
        item_copy["rrf_score"] = adjusted
        scored.append(item_copy)
    return sorted(scored, key=lambda x: -(x.get("score", 0.0)))


def _select_best_calorie_set(
    candidates: list["MealCandidateItem"],
    *,
    calorie_target: float | None,
    meals_per_day: int,
    tolerance: float,
) -> tuple[list["MealCandidateItem"], float | None, float | None, str | None]:
    """
    Phase 3: choose a meal set whose total calories best matches target.
    Returns reordered candidates (selected first), selected_total, delta, compliance.
    """
    if not candidates or calorie_target is None or calorie_target <= 0:
        return candidates, None, None, None

    meals = max(1, meals_per_day)
    pool = candidates[: min(len(candidates), 20)]
    indexed = [(idx, c, _safe_float(c.calories)) for idx, c in enumerate(pool)]
    with_cal = [(idx, c, cal) for idx, c, cal in indexed if cal is not None]
    if len(with_cal) < meals:
        return candidates, None, None, "partial"

    best_combo: tuple[int, ...] | None = None
    best_delta = float("inf")
    best_total: float | None = None

    for combo in combinations(range(len(with_cal)), meals):
        total = sum(float(with_cal[i][2]) for i in combo)
        delta = abs(total - calorie_target)
        if delta < best_delta:
            best_delta = delta
            best_combo = combo
            best_total = total

    if best_combo is None:
        return candidates, None, None, "partial"

    selected_ids = {with_cal[i][1].recipe_id for i in best_combo}
    selected = [c for c in candidates if c.recipe_id in selected_ids]
    non_selected = [c for c in candidates if c.recipe_id not in selected_ids]
    ordered = selected + non_selected

    delta_signed = (best_total - calorie_target) if best_total is not None else None
    compliance = None
    if delta_signed is not None:
        compliance = "adequate" if abs(delta_signed) <= tolerance else "partial"
    if compliance == "adequate":
        logger.info(
            "Calorie set selection marked adequate compliance",
            extra={
                "component": "calorie_set_selection",
                "counter": "calorie_set_selection_adequate_count",
                "value": 1,
            },
        )
    return ordered, best_total, delta_signed, compliance


def _merge_results_with_profile(
    fused: list[dict[str, Any]],
    entities: dict[str, Any],
    profile: dict[str, Any],
    *,
    driver: Driver,
    database: str | None = None,
    label: str = "Recipe",
    limit: int = 20,
    calorie_cache: dict[str, float | None] | None = None,
) -> list[RecommendationResult]:
    """
    Build RecommendationResult list from raw fused RRF results for personalized endpoints.
    Used by /recommend/feed and /recommend/meal-candidates (bypasses OrchestratorResult).

    Always returns results with PostgreSQL UUID. When UUID is not in payload,
    performs Neo4j lookup by elementId or title. Never skips recipes.
    """
    fused = fused[:limit]

    candidates: list[dict[str, Any]] = []
    for item in fused:
        payload = item.get("payload", {}) or {}
        key = item.get("key", "")
        title = payload.get("title") or item.get("title") or payload.get("name") or key
        rec_id = _resolve_id_with_lookup(
            payload,
            key,
            item,
            driver,
            label=label,
            database=database,
        )
        candidates.append({"item": item, "title": title, "rec_id": rec_id})

    recipe_ids_to_fetch = [str(c["rec_id"]) for c in candidates if _is_uuid(str(c["rec_id"]))]  # type: ignore[arg-type]
    recipe_ids_to_fetch = list(dict.fromkeys(recipe_ids_to_fetch))  # preserve order, dedupe

    ingredient_names_map = _fetch_recipe_ingredient_names(
        driver,
        recipe_ids_to_fetch,
        database=database,
    )
    graph_calories_map = _fetch_recipe_calories_map(
        driver,
        recipe_ids_to_fetch,
        database=database,
        request_cache=calorie_cache,
    )

    food_groups_map: dict[str, dict[str, Any]] = {}
    for rid, ing_names in ingredient_names_map.items():
        food_groups_map[rid] = infer_food_groups_for_ingredients(ing_names or [])

    out: list[RecommendationResult] = []
    missing_food_group_fields_count = 0
    for c in candidates:
        item = c["item"]
        rec_id = c["rec_id"]
        rid = str(rec_id)
        inferred = food_groups_map.get(rid) or {}
        payload = item.get("payload", {}) or {}
        graph_calories = graph_calories_map.get(rid)
        payload_calories = _safe_float(payload.get("calories"))
        resolved_calories = graph_calories if graph_calories is not None else payload_calories
        calorie_source = (
            "graph"
            if graph_calories is not None
            else ("payload_fallback" if payload_calories is not None else "unknown")
        )

        # Prefer well-formed payload food_groups when available; otherwise fall back
        # to inferred ingredient-based buckets. Non-list values are ignored.
        payload_food_groups = payload.get("food_groups")
        if not isinstance(payload_food_groups, list):
            missing_food_group_fields_count += 1
            payload_food_groups = None
        effective_food_groups = payload_food_groups or inferred.get("food_groups") or []
        coverage, balance_hint = _food_group_coverage_and_hint(effective_food_groups)

        out.append(
            RecommendationResult(
                id=rid,
                score=float(item.get("rrf_score", 0.0)),
                reasons=_build_reasons(item, entities, profile),
                metadata={
                    "title": c["title"],
                    "label": item.get("label", ""),
                    "sources": item.get("sources", []),
                    "id_source": "uuid" if _is_uuid(rec_id) else "lookup",
                    "calories": resolved_calories,
                    "calorie_source": calorie_source,
                    # Phase A contract: inferred food buckets for recipes.
                    "food_groups": effective_food_groups,
                    "food_group_coverage": coverage,
                    "usda_balance_hint": balance_hint,
                    "food_group_confidence": inferred.get("confidence_by_group") or {},
                    "food_group_source": "ingredient_keyword_rules",
                    "food_group_unknown_count": inferred.get("unknown_count"),
                    # Preserve any precomputed payload fields (future wiring).
                    "payload_food_groups": payload_food_groups,
                },
            )
        )

    if missing_food_group_fields_count:
        logger.info(
            "Food-group payload fields missing; inferred fallback used",
            extra={
                "component": "usda_food_groups",
                "counter": "food_group_inference_missing_fields_count",
                "value": missing_food_group_fields_count,
            },
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
    calorie_cache: dict[str, float | None] | None = None,
) -> list[RecommendationResult]:
    """
    Merge fused RRF results into a ranked list for API response.
    Uses OrchestratorResult.fused_results (already RRF-fused by apply_rrf()).

    Always returns PostgreSQL UUID. When not in payload, performs Neo4j lookup.
    Never returns title or elementId as id.
    """
    fused = orch.fused_results[:limit]

    candidates: list[dict[str, Any]] = []
    for item in fused:
        payload = item.get("payload", {}) or {}
        key = item.get("key", "")
        title = payload.get("title") or item.get("title") or payload.get("name") or key
        rec_id = _resolve_id_with_lookup(
            payload,
            key,
            item,
            driver,
            label=label,
            database=database,
        )
        candidates.append({"item": item, "title": title, "rec_id": rec_id})

    recipe_ids_to_fetch = [str(c["rec_id"]) for c in candidates if _is_uuid(str(c["rec_id"]))]  # type: ignore[arg-type]
    recipe_ids_to_fetch = list(dict.fromkeys(recipe_ids_to_fetch))  # preserve order, dedupe

    ingredient_names_map = _fetch_recipe_ingredient_names(
        driver,
        recipe_ids_to_fetch,
        database=database,
    )
    graph_calories_map = _fetch_recipe_calories_map(
        driver,
        recipe_ids_to_fetch,
        database=database,
        request_cache=calorie_cache,
    )

    food_groups_map: dict[str, dict[str, Any]] = {}
    for rid, ing_names in ingredient_names_map.items():
        food_groups_map[rid] = infer_food_groups_for_ingredients(ing_names or [])

    out: list[RecommendationResult] = []
    missing_food_group_fields_count = 0
    for c in candidates:
        item = c["item"]
        rec_id = c["rec_id"]
        rid = str(rec_id)
        inferred = food_groups_map.get(rid) or {}
        payload = item.get("payload", {}) or {}
        graph_calories = graph_calories_map.get(rid)
        payload_calories = _safe_float(payload.get("calories"))
        resolved_calories = graph_calories if graph_calories is not None else payload_calories
        calorie_source = (
            "graph"
            if graph_calories is not None
            else ("payload_fallback" if payload_calories is not None else "unknown")
        )

        payload_food_groups = payload.get("food_groups")
        if not isinstance(payload_food_groups, list):
            missing_food_group_fields_count += 1
            payload_food_groups = None
        effective_food_groups = payload_food_groups or inferred.get("food_groups") or []
        coverage, balance_hint = _food_group_coverage_and_hint(effective_food_groups)

        out.append(
            RecommendationResult(
                id=rid,
                score=float(item.get("rrf_score", 0.0)),
                reasons=_build_reasons(item, orch.entities, profile=None),
                metadata={
                    "title": c["title"],
                    "label": item.get("label", ""),
                    "sources": item.get("sources", []),
                    "id_source": "uuid" if _is_uuid(rec_id) else "lookup",
                    "calories": resolved_calories,
                    "calorie_source": calorie_source,
                    "food_groups": effective_food_groups,
                    "food_group_coverage": coverage,
                    "usda_balance_hint": balance_hint,
                    "food_group_confidence": inferred.get("confidence_by_group") or {},
                    "food_group_source": "ingredient_keyword_rules",
                    "food_group_unknown_count": inferred.get("unknown_count"),
                    "payload_food_groups": payload_food_groups,
                },
            )
        )

    if missing_food_group_fields_count:
        logger.info(
            "Food-group payload fields missing; inferred fallback used",
            extra={
                "component": "usda_food_groups",
                "counter": "food_group_inference_missing_fields_count",
                "value": missing_food_group_fields_count,
            },
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
    logger.info("search_hybrid context=%s", req.context)

    # Run NLU first (needed for family_scope / target_member_role from query)
    nlu_result = extract_hybrid(req.query)
    nlu_result.entities = _inject_calorie_limit_entity(req.query, nlu_result.entities or {})

    # ── Search-context intent override ────────────────────────────────────
    # On the search page, every query is a recipe search by definition.
    # The NLU may classify single-word queries like "swedish", "holiday",
    # "grandma's" as out_of_scope because they lack food-context keywords.
    # Override to find_recipe so all retrieval lanes (keyword, cypher,
    # semantic, structural) remain active.
    _NON_SEARCH_INTENTS = {"out_of_scope", "greeting", "help", "farewell", "unclear"}
    if nlu_result.intent in _NON_SEARCH_INTENTS:
        logger.info(
            "Search-context intent override: %s → find_recipe (query=%s)",
            nlu_result.intent, req.query,
        )
        nlu_result.intent = "find_recipe"
        # Preserve any extracted entities; add dish fallback if empty
        if not nlu_result.entities or not any(
            k in nlu_result.entities for k in ("dish", "diet", "course", "cuisine", "include_ingredient")
        ):
            nlu_result.entities["dish"] = req.query

    logger.warning(
        "search_hybrid NLU query=%s intent=%s entities=%s",
        req.query,
        nlu_result.intent,
        nlu_result.entities,
    )
    # Step 7: deterministic meal filter wiring from structured request filters.
    # Request filter takes precedence over NLU-derived course.
    meal_type_filter = req.filters.get("meal_type") if isinstance(req.filters, dict) else None
    if meal_type_filter and isinstance(meal_type_filter, str) and meal_type_filter.strip():
        nlu_result.entities["course"] = meal_type_filter.strip().lower()


    # Resolve profile: B2C (member_profile) primary, Neo4j fallback via merge
    customer_profile = None
    if req.customer_id:
        effective_scope_for_profile = (req.scope or "").strip() or nlu_result.entities.get("family_scope")
        if nlu_result.entities.get("target_member_role") and not effective_scope_for_profile:
            effective_scope_for_profile = "family"
        if not effective_scope_for_profile:
            effective_scope_for_profile = _infer_default_scope(
                _driver, req.customer_id, req.household_id, database,
                household_type_override=req.household_type,
            )
        if req.member_profile and isinstance(req.member_profile, dict):
            b2c_profile = _member_profile_to_profile(req.member_profile)
            neo4j_profile = _resolve_profile(
                _driver,
                req.customer_id,
                database,
                household_id=req.household_id,
                scope=effective_scope_for_profile,
                member_id=req.member_id,
                member_profile=None,
                family_scope=nlu_result.entities.get("family_scope"),
                target_member_role=nlu_result.entities.get("target_member_role"),
                household_type_override=req.household_type,
            )
            customer_profile = _merge_b2c_with_neo4j(b2c_profile, neo4j_profile)
        else:
            customer_profile = _resolve_profile(
                _driver,
                req.customer_id,
                database,
                household_id=req.household_id,
                scope=effective_scope_for_profile,
                member_profile=None,
                member_id=req.member_id,
                family_scope=nlu_result.entities.get("family_scope"),
                target_member_role=nlu_result.entities.get("target_member_role"),
                household_type_override=req.household_type,
            )

    effective_scope = (req.scope or "").strip() or None
    if not effective_scope:
        effective_scope = nlu_result.entities.get("family_scope")
    if not effective_scope and nlu_result.entities.get("target_member_role"):
        effective_scope = "family"
    if not effective_scope:
        effective_scope = _infer_default_scope(
            _driver, req.customer_id, req.household_id, database,
            household_type_override=req.household_type,
        )
    is_aggregated = _is_aggregated_profile(
        scope=effective_scope,
        family_scope=nlu_result.entities.get("family_scope"),
        target_member_role=nlu_result.entities.get("target_member_role"),
    )
    # PRD-33 Step 10: Attach context to profile before orchestrate (incl. anonymous search)
    if req.context:
        if customer_profile is None:
            customer_profile = {"context": req.context}
        else:
            customer_profile["context"] = req.context
    result = await orchestrate(
        _driver,
        cfg=_cfg,
        embedder=_embedder,
        user_query=req.query,
        customer_node_id=req.customer_id,
        customer_profile=customer_profile,
        is_aggregated_profile=is_aggregated,
        top_k=req.limit,
        database=database,
        intent_override=nlu_result.intent,
        entities_override=nlu_result.entities,
        usda_guidelines=_usda_guidelines,
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
    logger.info("recommend_feed context=%s", req.context)

    effective_scope = (req.scope or "").strip() or _infer_default_scope(
        _driver, req.customer_id, req.household_id, database,
        household_type_override=req.household_type,
    )
    # B2C primary: member_profile > preferences; merge with Neo4j for gaps (fallback)
    b2c_profile: dict[str, Any] | None = None
    if req.member_profile and isinstance(req.member_profile, dict):
        b2c_profile = _member_profile_to_profile(req.member_profile)
    elif req.preferences and isinstance(req.preferences, dict):
        b2c_profile = _preferences_to_profile(req.preferences)
    if b2c_profile:
        neo4j_profile = _resolve_profile(
            _driver,
            req.customer_id,
            database,
            household_id=req.household_id,
            scope=effective_scope,
            member_id=req.member_id,
            household_type_override=req.household_type,
        )
        profile = _merge_b2c_with_neo4j(b2c_profile, neo4j_profile)
        # Apply household_type override from request
        if req.household_type and req.household_type.strip().lower() in ("individual", "couple", "family"):
            profile["household_type"] = req.household_type.strip().lower()
    else:
        profile = _resolve_profile(
            _driver,
            req.customer_id,
            database,
            household_id=req.household_id,
            scope=effective_scope,
            member_id=req.member_id,
            member_profile=None,
            household_type_override=req.household_type,
        )

    # Resolve diet/allergen IDs (UUIDs) to names so Cypher and semantic get meaningful values
    profile = _resolve_profile_ids_to_names(_driver, profile, database)
    if req.context:
        profile["context"] = req.context

    entities: dict[str, Any] = {
        "diet":               profile["diets"],
        "exclude_ingredient": profile["allergens"],
    }
    if req.meal_type:
        entities["course"] = req.meal_type
    entities = merge_profile_into_entities(entities, profile)

    synthetic_text = build_feed_query_text(profile, req.meal_type, entities=entities)

    # DEBUG: feed profile/entities/synthetic (remove after diet debugging)
    logger.warning(
        "recommend_feed DEBUG profile.diets=%s profile.allergens=%s entities=%s synthetic_text=%s",
        profile.get("diets"),
        profile.get("allergens"),
        entities,
        synthetic_text,
    )

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

    # ── Structural or similar-constraint retrieval ─────────────────────────
    structural_results: dict[str, Any] = {}
    is_aggregated = _is_aggregated_profile(scope=effective_scope)
    try:
        if is_aggregated:
            structural_results = retrieve_recipes_from_similar_constraint_users(
                _driver,
                diets=profile["diets"],
                allergens=profile["allergens"],
                health_conditions=profile["health_conditions"],
                top_k=req.limit,
                database=database,
            )
        else:
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
        logger.warning("recommend_feed: structural/similar-constraint retrieval failed: %s", e)

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

    # DEBUG: feed fused items sources/title (remove after diet debugging)
    for i, item in enumerate(fused[:10]):
        payload = item.get("payload") or {}
        title = payload.get("title") or payload.get("name") or item.get("key") or ""
        logger.warning(
            "recommend_feed DEBUG fused[%s] sources=%s title=%s",
            i,
            item.get("sources"),
            title[:60] if title else "",
        )

    # ── Hard constraints: allergens/exclude_ingredient, course, calories ───
    fused = apply_hard_constraints(
        fused, entities, "find_recipe", _driver, database=database,
    )

    # ── Contextual rerank: recent meals, calorie target, cuisine preference ───
    fused = contextual_rerank(fused, entities)

    # DEBUG: feed after hard constraints (remove after diet debugging)
    after_titles = []
    for item in fused[:10]:
        p = item.get("payload") or {}
        t = p.get("title") or p.get("name") or item.get("key") or ""
        after_titles.append((t[:50] if t else "") or "(no title)")
    logger.warning(
        "recommend_feed DEBUG after_hard_constraints count=%s titles=%s",
        len(fused),
        after_titles,
    )

    # ── Post-filter: exclude recent meals (by recipe ID when from context, else by title) ─
    exclude_recipe_ids = entities.get("exclude_recipe_ids")
    if not isinstance(exclude_recipe_ids, list):
        exclude_recipe_ids = []
    if exclude_recipe_ids:
        exclude_ids = {str(rid).strip().lower() for rid in exclude_recipe_ids if rid}
        if exclude_ids:

            def _should_exclude(item: dict[str, Any]) -> bool:
                payload = item.get("payload") or {}
                key = item.get("key", "")
                rec_id = _resolve_id_with_lookup(
                    payload, key, item, _driver, label="Recipe", database=database
                )
                return rec_id is not None and str(rec_id).lower() in exclude_ids

            fused = [f for f in fused if not _should_exclude(f)]
    else:
        recent = {t.lower() for t in (profile.get("recent_recipes") or []) if t}
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
    logger.info("recommend_meal_candidates context=%s", req.context)

    effective_scope = (req.scope or "").strip() or _infer_default_scope(
        _driver, req.customer_id, req.household_id, database,
        household_type_override=req.household_type,
    )
    # B2C primary: members[] > member_profile; merge with Neo4j for gaps
    limit = req.limit
    if req.meals_per_day and isinstance(req.meals_per_day, int) and req.meals_per_day > 0 and req.date_range:
        # Scale limit by date_range days × meals_per_day for meal plan coverage
        start_d = req.date_range.get("start") or req.date_range.get("startDate")
        end_d = req.date_range.get("end") or req.date_range.get("endDate")
        if start_d and end_d:
            try:
                from datetime import datetime
                d1 = datetime.strptime(str(start_d)[:10], "%Y-%m-%d")
                d2 = datetime.strptime(str(end_d)[:10], "%Y-%m-%d")
                days = max(1, (d2 - d1).days + 1)
                limit = min(req.limit * 2, max(req.limit, days * req.meals_per_day))
                limit = min(100, max(50, int(limit)))
            except Exception:
                pass

    b2c_profile: dict[str, Any] | None = None
    if req.members and isinstance(req.members, list) and len(req.members) > 0:
        member_profiles = _members_to_profiles(req.members)
        b2c_profile = aggregate_profile(member_profiles)
    elif req.member_profile and isinstance(req.member_profile, dict):
        b2c_profile = _member_profile_to_profile(req.member_profile)
    if b2c_profile:
        neo4j_profile = _resolve_profile(
            _driver,
            req.customer_id,
            database,
            household_id=req.household_id,
            scope=effective_scope,
            member_id=req.member_id,
            member_profile=None,
            household_type_override=req.household_type,
        )
        profile = _merge_b2c_with_neo4j(b2c_profile, neo4j_profile)
        if req.household_type and req.household_type.strip().lower() in ("individual", "couple", "family"):
            profile["household_type"] = req.household_type.strip().lower()
    else:
        profile = _resolve_profile(
            _driver,
            req.customer_id,
            database,
            household_id=req.household_id,
            scope=effective_scope,
            member_id=req.member_id,
            member_profile=None,
            household_type_override=req.household_type,
        )

    profile = _resolve_profile_ids_to_names(_driver, profile, database)
    if req.context:
        profile["context"] = req.context

    entities: dict[str, Any] = {
        "diet": profile["diets"],
        "exclude_ingredient": profile["allergens"],
    }
    if isinstance(_usda_guidelines, dict):
        entities["usda_guidelines"] = _usda_guidelines
    if req.meal_type:
        entities["course"] = req.meal_type
    entities = merge_profile_into_entities(entities, profile)

    synthetic_text = build_feed_query_text(profile, req.meal_type, entities=entities)

    # ── Semantic retrieval ────────────────────────────────────────────────
    semantic_results: list[Any] = []
    try:
        semantic_results = retrieve_semantic(
            _driver,
            cfg=_cfg,
            embedder=_embedder,
            request=SemanticRetrievalRequest(
                query=synthetic_text,
                top_k=limit,
                label="Recipe",
            ),
            database=database,
        )
    except Exception as e:
        logger.warning("recommend_meal_candidates: semantic retrieval failed: %s", e)

    # ── Structural or similar-constraint retrieval ─────────────────────────
    structural_results: dict[str, Any] = {}
    is_aggregated = _is_aggregated_profile(scope=effective_scope)
    try:
        if is_aggregated:
            structural_results = retrieve_recipes_from_similar_constraint_users(
                _driver,
                diets=profile["diets"],
                allergens=profile["allergens"],
                health_conditions=profile["health_conditions"],
                top_k=limit,
                database=database,
            )
        else:
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
                    top_k=limit,
                    allowed_labels=["Recipe"],
                    allowed_relationships=["SAVED", "VIEWED"],
                    database=database,
                )
    except Exception as e:
        logger.warning("recommend_meal_candidates: structural/similar-constraint retrieval failed: %s", e)

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
        max_items=limit,
    )

    # ── Hard constraints: allergens/exclude_ingredient, course, calories ───
    fused = apply_hard_constraints(
        fused, entities, "find_recipe", _driver, database=database,
    )
    fused = apply_usda_food_group_bonus(fused, entities, "find_recipe")
    fused = contextual_rerank(fused, entities)

    # ── Post-filter: exclude recently eaten + meal_history + exclude_ids ───
    exclude_ids = {rid.strip().lower() for rid in (req.meal_history or []) + (req.exclude_ids or []) if rid}
    ctx_exclude = entities.get("exclude_recipe_ids")
    if isinstance(ctx_exclude, list) and ctx_exclude:
        exclude_ids |= {str(rid).strip().lower() for rid in ctx_exclude if rid}
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

    expected_meals = req.meals_per_day if isinstance(req.meals_per_day, int) and req.meals_per_day > 0 else 3
    calorie_target_raw = None
    if isinstance(req.member_profile, dict):
        calorie_target_raw = req.member_profile.get("calorie_target")
    if calorie_target_raw is None and isinstance(profile, dict):
        calorie_target_raw = profile.get("calorie_target")
    calorie_target = _safe_float(calorie_target_raw)
    calorie_tolerance = (max(100.0, (calorie_target or 0.0) * 0.08) if calorie_target else None)

    calorie_cache: dict[str, float | None] = {}
    # Phase 2: calorie-aware soft rerank before response mapping.
    if os.getenv("ENABLE_CALORIE_RERANK", "1").strip() == "1":
        fused = _inject_graph_calories_into_fused(
            fused,
            driver=_driver,
            database=database,
            label="Recipe",
            calorie_cache=calorie_cache,
        )
        fused = _apply_calorie_fit_rerank(
            fused,
            calorie_target=calorie_target,
            meals_per_day=expected_meals,
        )

    # ── Map to MealCandidateItem format ────────────────────────────────────
    recs = _merge_results_with_profile(
        fused, entities, profile,
        driver=_driver,
        database=database,
        label="Recipe",
        limit=limit,
        calorie_cache=calorie_cache,
    )
    candidates = [
        MealCandidateItem(
            recipe_id=r.id,
            title=r.metadata.get("title", r.id),
            score=r.score,
            reasons=r.reasons,
            food_groups=r.metadata.get("food_groups") or [],
            calories=_safe_float(r.metadata.get("calories")),
        )
        for r in recs
    ]

    # Phase 3: select best daily set by calorie closeness and reorder candidates.
    selected_total_calories: float | None = None
    calorie_delta: float | None = None
    calorie_compliance: str | None = None
    if os.getenv("ENABLE_CALORIE_SET_SELECTION", "1").strip() == "1":
        candidates, selected_total_calories, calorie_delta, calorie_compliance = _select_best_calorie_set(
            candidates,
            calorie_target=calorie_target,
            meals_per_day=expected_meals,
            tolerance=(calorie_tolerance or 0.0),
        )

    guideline_compliance: str | None = None
    audit_warnings: list[str] = []
    missing_groups: list[str] = []
    food_group_audit: list[dict[str, Any]] = []
    strict_mode_insufficiency: dict[str, Any] | None = None
    strict_mode_enabled = (
        os.getenv("USDA_STRICT_MODE", "").strip() == "1"
        or os.getenv("USDA_GUIDELINES_STRICT", "").strip() == "1"
    )
    try:
        candidate_rows = [
            {
                "recipe_id": c.recipe_id,
                "title": c.title,
                "food_groups": c.food_groups,
            }
            for c in candidates[:expected_meals]
        ]

        audit = audit_candidate_set(
            candidate_rows,
            usda_guidelines=_usda_guidelines,
            calorie_target=calorie_target,
            expected_meals=expected_meals,
        )
        food_group_audit = audit.get("food_group_audit") or []
        missing_groups = audit.get("missing_groups") or []
        if missing_groups:
            guideline_compliance = "partial"
            audit_warnings = build_audit_warnings(missing_groups)
            if strict_mode_enabled:
                strict_mode_insufficiency = {
                    "reason": "insufficient_food_group_coverage",
                    "guideline_compliance": "partial",
                    "missing_groups": missing_groups,
                    "expected_meals": expected_meals,
                    "calorie_target": calorie_target,
                    "message": (
                        "USDA strict mode is enabled. Returning best-feasible candidates "
                        "with explicit insufficiency metadata."
                    ),
                }
                audit_warnings.append(
                    "USDA strict mode is enabled; coverage remains infeasible for one or more food groups."
                )
            logger.info(
                "Meal candidate audit marked partial compliance",
                extra={
                    "component": "food_group_audit",
                    "counter": "meal_candidate_audit_partial_count",
                    "value": 1,
                },
            )
        else:
            guideline_compliance = "adequate"
    except Exception as e:
        logger.warning(
            "Meal candidate USDA audit failed; returning candidates without audit metadata: %s",
            e,
            extra={
                "component": "food_group_audit",
                "counter": "meal_candidate_audit_fail_count",
                "value": 1,
            },
        )

    zero_explanation = None
    if not candidates:
        zero_explanation = build_zero_results_message(entities, "find_recipe")
    latency_ms = (time.time() - start) * 1000
    logger.info("recommend_meal_candidates complete", extra={"endpoint": "recommend_meal_candidates", "identity": f"{identity[:8]}..." if len(identity) > 8 else identity, "latency_ms": round(latency_ms)})
    return MealCandidateResponse(
        candidates=candidates,
        retrieval_time_ms=latency_ms,
        zero_results_explanation=zero_explanation,
        guideline_compliance=guideline_compliance,
        audit_warnings=audit_warnings,
        missing_groups=missing_groups,
        food_group_audit=food_group_audit,
        strict_mode_insufficiency=strict_mode_insufficiency,
        daily_calorie_target=calorie_target,
        selected_total_calories=selected_total_calories,
        calorie_delta=calorie_delta,
        calorie_tolerance=calorie_tolerance,
        calorie_compliance=calorie_compliance,
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
        ingredient_names=req.ingredient_names,
        customer_allergens=req.customer_allergens or [],
        quality_preferences=req.quality_preferences,
        preferred_brands=req.preferred_brands,
        household_budget=req.household_budget,
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
    logger.info("chat_process context=%s", req.context)
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

    # NLU: intent + entities (rules first, LLM fallback). Pass history for session-based
    # intent continuation (e.g. "more options?" after substitution -> get_substitution_suggestion)
    nlu_result = extract_hybrid(effective_msg, context={"history": history_pairs})
    nlu_result.entities = _inject_calorie_limit_entity(effective_msg, nlu_result.entities or {})

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
        if req.member_profile and isinstance(req.member_profile, dict):
            b2c_profile = _member_profile_to_profile(req.member_profile)
            neo4j_profile = _resolve_profile(
                _driver,
                req.customer_id,
                database,
                household_id=req.household_id,
                member_profile=None,
                member_id=req.member_id,
                family_scope=nlu_result.entities.get("family_scope"),
                target_member_role=nlu_result.entities.get("target_member_role"),
                household_type_override=req.household_type,
            )
            profile = _merge_b2c_with_neo4j(b2c_profile, neo4j_profile)
        else:
            profile = _resolve_profile(
                _driver,
                req.customer_id,
                database,
                household_id=req.household_id,
                member_profile=None,
                member_id=req.member_id,
                family_scope=nlu_result.entities.get("family_scope"),
                target_member_role=nlu_result.entities.get("target_member_role"),
                household_type_override=req.household_type,
            )
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
            chat_scope = nlu_result.entities.get("family_scope") or (
                "family" if nlu_result.entities.get("target_member_role") else None
            )
            if not chat_scope:
                chat_scope = _infer_default_scope(
                    _driver, req.customer_id, req.household_id, database,
                    household_type_override=req.household_type,
                )
            if req.member_profile and isinstance(req.member_profile, dict):
                b2c_profile = _member_profile_to_profile(req.member_profile)
                neo4j_profile = _resolve_profile(
                    _driver,
                    req.customer_id,
                    database,
                    household_id=req.household_id,
                    scope=chat_scope,
                    member_profile=None,
                    member_id=req.member_id,
                    family_scope=nlu_result.entities.get("family_scope"),
                    target_member_role=nlu_result.entities.get("target_member_role"),
                    household_type_override=req.household_type,
                )
                profile = _merge_b2c_with_neo4j(b2c_profile, neo4j_profile)
            else:
                profile = _resolve_profile(
                    _driver,
                    req.customer_id,
                    database,
                    household_id=req.household_id,
                    scope=chat_scope,
                    member_profile=None,
                    member_id=req.member_id,
                    family_scope=nlu_result.entities.get("family_scope"),
                    target_member_role=nlu_result.entities.get("target_member_role"),
                    household_type_override=req.household_type,
                )
            if req.context:
                profile["context"] = req.context

            # For substitution follow-ups, use synthesized query if effective_msg is
            # still vague (orchestrator re-extracts intent; "some more options?" is unclear)
            orch_query = effective_msg
            if nlu_result.intent == "get_substitution_suggestion":
                ing = nlu_result.entities.get("ingredient")
                if ing and len(effective_msg.split()) <= 15 and ing.lower() not in effective_msg.lower():
                    orch_query = f"What are more substitutes for {ing}?"

            # Pass intent/entities from chat NLU — skip LLM extraction in orchestrator.
            # Avoids duplicate LLM calls and failures when LLM is down (chat rules still work).
            is_aggregated = _is_aggregated_profile(
                scope=chat_scope,
                family_scope=nlu_result.entities.get("family_scope"),
                target_member_role=nlu_result.entities.get("target_member_role"),
            )
            orch_result = await orchestrate(
                _driver,
                cfg=_cfg,
                embedder=_embedder,
                user_query=orch_query,
                customer_node_id=req.customer_id,
                customer_profile=profile,
                is_aggregated_profile=is_aggregated,
                top_k=10,
                database=database,
                intent_override=nlu_result.intent,
                entities_override=nlu_result.entities,
                usda_guidelines=_usda_guidelines,
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
                orch_query,
                history_text,
                customer_profile=profile,
                temperature=0.3,
                max_fused=10,
            )
            if _response_validation_cfg.get("enabled", False):
                _response = sanitize_response(
                    _response,
                    profile,
                    intent=nlu_result.intent,
                    config=_response_validation_cfg,
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
