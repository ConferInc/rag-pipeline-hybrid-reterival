"""
B2B API routes and schemas.
Vendor-scoped endpoints for NutriB2B platform.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from neo4j import Driver
from pydantic import BaseModel, Field

from .b2b_cypher import (
    build_b2b_customers_with_condition,
    build_b2b_product_customers,
    build_b2b_product_intel,
    build_b2b_products_allergen_free,
    build_b2b_products_for_condition,
    build_b2b_products_for_diet,
    build_b2b_recommend_products,
    build_b2b_safety_check,
    build_b2b_search_products,
    build_b2b_substitutions,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/b2b", tags=["b2b"])


# ── Helpers (driver injected at request time to avoid circular import) ──────

def _get_driver() -> Driver:
    from api.app import _driver
    if _driver is None:
        raise HTTPException(status_code=503, detail="Neo4j not initialized")
    return _driver


# ── Schemas ─────────────────────────────────────────────────────────────────

class HealthProfileInput(BaseModel):
    target_calories: int | None = None
    target_protein_g: float | None = None
    bmi: float | None = None
    derived_limits: dict[str, float] | None = None  # Handoff: sodium_mg, sugar_g, etc.
    activity_level: str | None = None
    health_goal: str | None = None


class RecommendProductsRequest(BaseModel):
    b2b_customer_id: str = Field(..., description="B2B customer UUID")
    vendor_id: str = Field(..., description="Vendor UUID (required)")
    allergens: list[str] = Field(default_factory=list)
    health_conditions: list[str] = Field(default_factory=list)
    dietary_preferences: list[str] = Field(default_factory=list)
    health_profile: HealthProfileInput | dict | None = None
    limit: int = Field(20, ge=1, le=100)
    filters: dict[str, Any] = Field(default_factory=dict)


class ProductRecommendationItem(BaseModel):
    id: str
    name: str
    brand: str = ""
    score: float
    reasons: list[str] = Field(default_factory=list)
    calories: float | None = None
    protein_g: float | None = None
    image_url: str | None = None


class RecommendProductsResponse(BaseModel):
    products: list[ProductRecommendationItem]
    explanation: str | None = None
    retrieval_time_ms: float


class ProductCustomersRequest(BaseModel):
    product_id: str = Field(..., description="Product UUID")
    vendor_id: str = Field(..., description="Vendor UUID (required)")
    limit: int = Field(50, ge=1, le=200)
    include_reasons: bool = True
    include_warnings: bool = True


class CustomerMatchItem(BaseModel):
    id: str = Field("", description="Alias for customer_id for Handoff consistency")
    customer_id: str
    customer_name: str
    email: str = ""
    match_score: float
    safety_status: str  # safe | warning
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    diets: list[str] = Field(default_factory=list)


class ProductCustomersSummary(BaseModel):
    total_customers: int = 0
    safe_count: int = 0
    warning_count: int = 0
    excluded_count: int = 0
    not_evaluated_count: int = 0


class ProductCustomersResponse(BaseModel):
    customers: list[CustomerMatchItem]
    summary: ProductCustomersSummary
    retrieval_time_ms: float


class SearchRequest(BaseModel):
    query: str = Field("", description="Natural language search (optional for filter-only)")
    vendor_id: str = Field(..., description="Vendor UUID (required)")
    filters: dict[str, Any] = Field(default_factory=dict)
    limit: int = Field(20, ge=1, le=50)


class SearchResultItem(BaseModel):
    id: str
    name: str
    brand: str = ""
    score: float
    reasons: list[str] = Field(default_factory=list)
    match_type: str | None = None


class SearchResponse(BaseModel):
    results: list[SearchResultItem]
    query_interpretation: str | None = None
    total_found: int = 0
    retrieval_time_ms: float


class SearchSuggestRequest(BaseModel):
    query: str = Field(..., min_length=1)
    vendor_id: str = Field(..., description="Vendor UUID (required)")


class SearchSuggestResponse(BaseModel):
    suggestions: list[str] = Field(default_factory=list)
    entities_found: dict[str, list[str]] = Field(default_factory=dict)
    entities_found_counts: dict[str, int] = Field(
        default_factory=dict,
        description="Entity counts per type for Handoff (e.g. products: 5, allergens: 1)",
    )


class SubstitutionsRequest(BaseModel):
    product_id: str = Field(..., description="Product UUID")
    vendor_id: str = Field(..., description="Vendor UUID (required)")
    customer_id: str | None = None
    limit: int = Field(10, ge=1, le=50)


class SubstituteItem(BaseModel):
    id: str
    name: str
    brand: str = ""
    score: float
    reason: str = Field("", description="Human-readable reason for frontend")
    reasons: list[str] = Field(default_factory=list)
    score_breakdown: dict[str, float] = Field(default_factory=dict)


class SubstitutionsResponse(BaseModel):
    original: dict[str, Any] | None = None
    substitutes: list[SubstituteItem]
    customer_context: dict[str, Any] | None = None
    retrieval_time_ms: float


class ProductIntelRequest(BaseModel):
    product_id: str = Field(..., description="Product UUID")
    vendor_id: str = Field(..., description="Vendor UUID (required)")


class DietCompatibilityItem(BaseModel):
    diet: str
    compatible: bool
    reason: str | None = None


class ProductIntelResponse(BaseModel):
    diet_compatibility: list[DietCompatibilityItem] = Field(default_factory=list)
    ingredients: list[str] = Field(default_factory=list)
    allergens: list[str] = Field(default_factory=list)
    customer_suitability: str | None = None
    retrieval_time_ms: float


class SafetyCheckRequest(BaseModel):
    vendor_id: str = Field(..., description="Vendor UUID (required)")
    product_ids: list[str] | None = None
    customer_ids: list[str] | None = None


class SafetyConflictItem(BaseModel):
    product_id: str
    product_name: str
    customer_id: str
    customer_name: str
    conflict_allergen: str
    allergen_code: str
    customer_severity: str


class SafetyCheckResponse(BaseModel):
    conflicts: list[SafetyConflictItem] = Field(default_factory=list)
    cross_reactive: list[dict[str, Any]] = Field(default_factory=list)
    summary: dict[str, int] = Field(default_factory=dict)
    summary_str: str | None = Field(None, description="Human-readable summary for frontend display")
    retrieval_time_ms: float


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    vendor_id: str = Field(..., description="Vendor UUID (required)")
    user_id: str | None = None
    session_id: str | None = None


class ChatResponse(BaseModel):
    response: str
    intent: str | None = None
    entities: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = None
    structured_data: dict[str, Any] | None = None
    report_data: list[dict[str, Any]] | None = Field(
        None,
        description="Array of row objects for CSV export (Handoff)",
    )


# ── Intent → Cypher routing ─────────────────────────────────────────────────

def route_b2b_intent(
    intent: str,
    entities: dict[str, Any],
    vendor_id: str,
    limit: int = 20,
) -> tuple[str | None, dict[str, Any] | None]:
    """
    Map B2B intent to the right cypher builder. Returns (cypher, params) or (None, None).
    """
    allergens = entities.get("allergens") or entities.get("exclude_ingredient") or []
    conditions = entities.get("health_conditions") or []
    diets = entities.get("diet") or []
    product_name = (entities.get("product_name") or "").strip()
    customer_name = (entities.get("customer_name") or "").strip()
    cal_limit = entities.get("cal_upper_limit")
    nutrient_thresh = entities.get("nutrient_threshold") or {}
    min_protein = nutrient_thresh.get("value") if nutrient_thresh.get("nutrient", "").lower() == "protein" and nutrient_thresh.get("operator") == "gt" else None

    if intent == "b2b_products_allergen_free":
        cypher, params = build_b2b_products_allergen_free(vendor_id, allergens, limit)
        return cypher, params

    if intent == "b2b_products_for_diet":
        cypher, params = build_b2b_products_for_diet(
            vendor_id, diets, limit,
            max_calories=cal_limit,
            min_protein=min_protein,
        )
        return cypher, params

    if intent == "b2b_products_for_condition":
        cypher, params = build_b2b_products_for_condition(vendor_id, conditions, limit)
        return cypher, params

    if intent == "b2b_customers_with_condition":
        cypher, params = build_b2b_customers_with_condition(
            vendor_id, conditions, limit=min(limit, 50),
        )
        return cypher, params

    # Intents requiring product_id or customer_id (resolve from name → id via search)
    if intent in ("b2b_product_compliance", "b2b_product_nutrition") and product_name:
        # Use search to find product by name, then product_intel or product_customers
        cypher, params = build_b2b_search_products(
            vendor_id, limit=5, category=product_name,
        )
        return cypher, params

    if intent == "b2b_customer_recommendations" and customer_name:
        # Would need customer name → id resolution; for now fallback to products_for_diet
        if diets or conditions or allergens:
            diets_used = diets or []
            if conditions:
                from .b2b_cypher import _CONDITION_TO_DIET
                for c in conditions:
                    for d in _CONDITION_TO_DIET.get(c, []):
                        if d and d not in diets_used:
                            diets_used.append(d)
            cypher, params = build_b2b_products_for_diet(vendor_id, diets_used or ["vegan"], limit)
            return cypher, params

    if intent == "b2b_analytics":
        # No dedicated builder; return products as fallback
        cypher, params = build_b2b_search_products(vendor_id, limit=limit)
        return cypher, params

    if intent == "b2b_generate_report":
        cypher, params = build_b2b_search_products(vendor_id, limit=limit)
        return cypher, params

    # Default: products for diet (generic product list)
    cypher, params = build_b2b_products_for_diet(vendor_id, diets or [], limit)
    return cypher, params


# ── Helpers ─────────────────────────────────────────────────────────────────

def _run_cypher(driver: Driver, cypher: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    database = os.getenv("NEO4J_DATABASE")
    try:
        with driver.session(database=database) as session:
            rows = session.run(cypher, **params)
            return [dict(row) for row in rows]
    except Exception as e:
        logger.warning("B2B Cypher failed", extra={"error": str(e), "params_keys": list(params.keys())})
        return []


# ── Routes ──────────────────────────────────────────────────────────────────

@router.post("/recommend-products", response_model=RecommendProductsResponse)
async def recommend_products(req: RecommendProductsRequest):
    """Recommend products for a B2B customer. Vendor-scoped."""
    start = time.time()
    driver = _get_driver()

    filters = req.filters or {}
    hp = req.health_profile
    target_cal = target_protein = None
    if isinstance(hp, dict):
        target_cal = hp.get("target_calories")
        target_protein = hp.get("target_protein_g")
        derived = hp.get("derived_limits") or {}
        if target_cal is None and "calories" in derived:
            target_cal = int(derived["calories"])
        if target_protein is None and "protein_g" in derived:
            target_protein = float(derived["protein_g"])
    elif hp:
        target_cal = hp.target_calories
        target_protein = hp.target_protein_g
        derived = (hp.derived_limits or {}) if hasattr(hp, "derived_limits") else {}
        if target_cal is None and "calories" in derived:
            target_cal = int(derived["calories"])
        if target_protein is None and "protein_g" in derived:
            target_protein = float(derived["protein_g"])

    cypher, params = build_b2b_recommend_products(
        vendor_id=req.vendor_id,
        customer_id=req.b2b_customer_id,
        allergen_codes=req.allergens,
        condition_codes=req.health_conditions,
        diet_codes=req.dietary_preferences,
        limit=req.limit,
        max_calories=filters.get("maxCalories") or target_cal,
        min_protein=filters.get("minProtein") or target_protein,
        category_id=filters.get("category"),
    )

    rows = _run_cypher(driver, cypher, params)
    products = [
        ProductRecommendationItem(
            id=str(r.get("id", "")),
            name=str(r.get("name", "")),
            brand=str(r.get("brand", "")),
            score=float(r.get("score", 0.8)),
            reasons=["Allergen-safe"] if req.allergens else ["Vendor product"],
            calories=r.get("calories"),
            protein_g=r.get("protein_g"),
            image_url=r.get("image_url"),
        )
        for r in rows
    ]

    elapsed = (time.time() - start) * 1000
    return RecommendProductsResponse(
        products=products,
        explanation=f"Found {len(products)} products for this customer." if products else None,
        retrieval_time_ms=elapsed,
    )


@router.post("/product-customers", response_model=ProductCustomersResponse)
async def product_customers(req: ProductCustomersRequest):
    """Find matching customers for a product. Vendor-scoped."""
    start = time.time()
    driver = _get_driver()

    cypher, params = build_b2b_product_customers(
        vendor_id=req.vendor_id,
        product_id=req.product_id,
        limit=req.limit,
        include_warnings=req.include_warnings,
    )

    rows = _run_cypher(driver, cypher, params)
    customers = []
    for r in rows:
        reasons = []
        warnings = []
        if req.include_reasons:
            reasons = ["No allergen conflicts"] if r.get("safety_status") == "safe" else ["Mild allergen overlap"]
        if req.include_warnings and r.get("safety_status") == "warning":
            warnings = ["Product may contain customer allergens — verify before recommending"]

        cid = str(r.get("customer_id", ""))
        customers.append(
            CustomerMatchItem(
                id=cid,
                customer_id=cid,
                customer_name=str(r.get("customer_name", "")),
                email=str(r.get("email", "")),
                match_score=float(r.get("match_score", 1.0)),
                safety_status=str(r.get("safety_status", "safe")),
                reasons=reasons,
                warnings=warnings,
                diets=[d for d in (r.get("diets") or []) if d],
            )
        )

    safe = sum(1 for c in customers if c.safety_status == "safe")
    warn = sum(1 for c in customers if c.safety_status == "warning")
    elapsed = (time.time() - start) * 1000
    return ProductCustomersResponse(
        customers=customers,
        summary=ProductCustomersSummary(
            total_customers=len(customers),
            safe_count=safe,
            warning_count=warn,
            excluded_count=0,
            not_evaluated_count=0,
        ),
        retrieval_time_ms=elapsed,
    )


@router.post("/search", response_model=SearchResponse)
async def search(req: SearchRequest):
    """NLP-powered product search. Vendor-scoped. Merges entities from query into filters."""
    start = time.time()
    driver = _get_driver()

    filters = dict(req.filters or {})
    nlu = None
    if req.query:
        from chatbot.nlu import extract_hybrid_b2b
        nlu = extract_hybrid_b2b(req.query)
        ents = nlu.entities or {}
        # Merge entities into filters
        if ents.get("diet"):
            filters["diets"] = ents["diet"]
        if ents.get("allergens"):
            filters["allergen_free"] = ents["allergens"]
        if ents.get("exclude_ingredient"):
            filters["allergen_free"] = filters.get("allergen_free") or []
            filters["allergen_free"] = list(set(filters["allergen_free"]) | set(ents["exclude_ingredient"]))
        if ents.get("cal_upper_limit") is not None:
            filters["maxCalories"] = ents["cal_upper_limit"]
        nt = ents.get("nutrient_threshold") or {}
        if nt.get("nutrient", "").lower() == "protein" and nt.get("operator") == "gt" and nt.get("value") is not None:
            filters["minProtein"] = nt["value"]
        if ents.get("category"):
            filters["category"] = ents["category"]

    cypher, params = build_b2b_search_products(
        vendor_id=req.vendor_id,
        max_calories=filters.get("maxCalories"),
        min_protein=filters.get("minProtein"),
        category=filters.get("category"),
        category_id=filters.get("category_id"),
        diet_codes=filters.get("diets"),
        allergen_free=filters.get("allergen_free"),
        brand=filters.get("brand"),
        status=filters.get("status"),
        limit=req.limit,
    )

    rows = _run_cypher(driver, cypher, params)
    results = [
        SearchResultItem(
            id=str(r.get("id", "")),
            name=str(r.get("name", "")),
            brand=str(r.get("brand", "")),
            score=float(r.get("score", 0.9)),
            reasons=[f"{r.get('protein_g')}g protein"] if r.get("protein_g") else [],
            match_type="structural",
        )
        for r in rows
    ]

    query_interpretation = None
    if req.query and nlu and nlu.entities:
        parts = []
        e = nlu.entities
        if e.get("diet"):
            parts.append(f"diets: {', '.join(e['diet'])}")
        if e.get("allergens") or e.get("exclude_ingredient"):
            al = e.get("allergens") or e.get("exclude_ingredient") or []
            parts.append(f"free from: {', '.join(al)}")
        if e.get("cal_upper_limit"):
            parts.append(f"max {e['cal_upper_limit']} cal")
        if e.get("nutrient_threshold"):
            nt = e["nutrient_threshold"]
            parts.append(f"{nt.get('operator','')} {nt.get('value','')}g {nt.get('nutrient','')}")
        query_interpretation = "; ".join(parts) if parts else f"Products matching: {req.query}"

    elapsed = (time.time() - start) * 1000
    return SearchResponse(
        results=results,
        query_interpretation=query_interpretation or (f"Products matching: {req.query}" if req.query else None),
        total_found=len(results),
        retrieval_time_ms=elapsed,
    )


@router.post("/search-suggest", response_model=SearchSuggestResponse)
async def search_suggest(req: SearchSuggestRequest):
    """Did You Mean? query suggestions. Extracts entities and builds contextual suggestions."""
    from chatbot.nlu import extract_hybrid_b2b

    nlu = extract_hybrid_b2b(req.query)
    entities = nlu.entities or {}
    suggestions: list[str] = []
    entities_found: dict[str, list[str]] = {}

    diets = entities.get("diet") or []
    conditions = entities.get("health_conditions") or []
    allergens = entities.get("allergens") or entities.get("exclude_ingredient") or []
    nt = entities.get("nutrient_threshold") or {}

    if diets:
        entities_found["diet"] = diets
        for d in diets:
            suggestions.append(f"Products compatible with {d.replace('_', ' ')} diet")
    if conditions:
        entities_found["health_conditions"] = conditions
        for c in conditions:
            suggestions.append(f"Products safe for customers with {c.replace('_', ' ')}")
    if allergens:
        entities_found["allergens"] = allergens
        for a in allergens:
            suggestions.append(f"Products free from {a.replace('_', ' ')}")
    if nt and nt.get("nutrient") and nt.get("value") is not None:
        entities_found["nutrient_threshold"] = [f"{nt.get('operator','')} {nt.get('value','')}g {nt.get('nutrient','')}"]
        op = nt.get("operator", "gt")
        val = nt.get("value", "")
        nut = nt.get("nutrient", "").lower()
        suggestions.append(f"Products with {op} {val}g {nut}")

    if not suggestions:
        suggestions = [
            f"Products matching '{req.query}'",
            "Products by diet (e.g. keto, vegan)",
            "Products by allergen (e.g. nut-free, dairy-free)",
            "Products by nutrition (e.g. high protein)",
        ]
        entities_found = {"query_terms": req.query.lower().split()}

    # Build counts for Handoff (entities_found has lists; counts are lengths)
    entities_found_counts = {k: len(v) if isinstance(v, list) else 0 for k, v in entities_found.items()}

    return SearchSuggestResponse(
        suggestions=suggestions[:8],
        entities_found=entities_found,
        entities_found_counts=entities_found_counts,
    )


@router.post("/substitutions", response_model=SubstitutionsResponse)
async def substitutions(req: SubstitutionsRequest):
    """Smart product substitution. Vendor-scoped."""
    start = time.time()
    driver = _get_driver()

    cypher, params = build_b2b_substitutions(
        vendor_id=req.vendor_id,
        product_id=req.product_id,
        customer_id=req.customer_id,
        limit=req.limit,
    )

    rows = _run_cypher(driver, cypher, params)
    substitutes = []
    for r in rows:
        # CHANGED: reason now reflects actual similarity signals instead of hardcoded string
        # OLD: reasons_list = [f"Similar nutrition ({r.get('calories')} cal)"]
        reasons_list = []
        shared = int(r.get("shared_count") or 0)
        if shared > 0:
            reasons_list.append(f"{shared} shared ingredient{'s' if shared > 1 else ''}")
        cal = r.get("calories")
        cal_sim = float(r.get("calorie_sim") or 0)
        if cal is not None and cal_sim >= 0.7:
            reasons_list.append(f"Similar calories ({cal} kcal)")
        protein_sim = float(r.get("protein_sim") or 0)
        if protein_sim >= 0.7 and r.get("protein_g") is not None:
            reasons_list.append(f"Similar protein ({r.get('protein_g')}g)")
        if not reasons_list:
            reasons_list.append("Same product category")
        reason = ", ".join(reasons_list)

        # CHANGED: score_breakdown now multi-factor instead of {"nutrition_similarity": score}
        # OLD: score_breakdown = {"nutrition_similarity": float(r.get("score", 0.8))}
        score_breakdown = {
            "ingredient_overlap": float(r.get("ingredient_jaccard") or 0),
            "calorie_similarity": cal_sim,
            "protein_similarity": protein_sim,
        }
        substitutes.append(
            SubstituteItem(
                id=str(r.get("id", "")),
                name=str(r.get("name", "")),
                brand=str(r.get("brand", "") or ""),
                # CHANGED: score was hardcoded 0.8; now comes from composite Cypher score
                # OLD: score=float(r.get("score", 0.8)),
                # OLD: score=float(r.get("score", 0.5)),  — 0.5 is arbitrary, would surface mediocre results
                score=float(r.get("score", 0.0)),
                reason=reason,
                reasons=reasons_list,
                score_breakdown=score_breakdown,
            )
        )

    elapsed = (time.time() - start) * 1000
    ctx = None
    if req.customer_id:
        ctx = {"customer_id": req.customer_id, "allergens_excluded": [], "note": "Health-aware substitution"}

    return SubstitutionsResponse(
        original=None,
        substitutes=substitutes,
        customer_context=ctx,
        retrieval_time_ms=elapsed,
    )


@router.post("/product-intel", response_model=ProductIntelResponse)
async def product_intel(req: ProductIntelRequest):
    """Diet compatibility, ingredients, allergens, and customer suitability for a product."""
    start = time.time()
    driver = _get_driver()

    cypher, params = build_b2b_product_intel(
        vendor_id=req.vendor_id,
        product_id=req.product_id,
    )

    rows = _run_cypher(driver, cypher, params)
    diet_compatibility: list[DietCompatibilityItem] = []
    ingredients: list[str] = []
    allergens: list[str] = []
    customer_suitability: str | None = None

    if rows:
        r = rows[0]
        comp = r.get("diet_compatibility") or []
        for d in comp:
            diet_compatibility.append(
                DietCompatibilityItem(diet=str(d), compatible=True, reason=None)
            )
        ingredients = [str(x) for x in (r.get("ingredients") or []) if x]
        allergens = [str(x) for x in (r.get("allergens") or []) if x]
        # Build customer_suitability from diets + allergens
        parts = []
        if diet_compatibility:
            diet_names = [d.diet for d in diet_compatibility]
            parts.append(f"Suitable for {', '.join(diet_names)} diets.")
        if allergens:
            parts.append(f"Avoid if allergic to: {', '.join(allergens)}.")
        if parts:
            customer_suitability = " ".join(parts)
        else:
            customer_suitability = "Suitable for most diets."

    elapsed = (time.time() - start) * 1000
    return ProductIntelResponse(
        diet_compatibility=diet_compatibility,
        ingredients=ingredients,
        allergens=allergens,
        customer_suitability=customer_suitability,
        retrieval_time_ms=elapsed,
    )


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """
    B2B domain chatbot. NLU → intent routing → Cypher → response.
    """
    import uuid
    from chatbot.b2b_session import add_message, get_or_create_session
    from chatbot.nlu import extract_hybrid_b2b

    start = time.time()
    session = get_or_create_session(req.session_id, req.vendor_id)
    session_id = session.session_id
    add_message(session_id, "user", req.message)

    nlu = extract_hybrid_b2b(req.message)
    intent = nlu.intent
    entities = nlu.entities or {}

    cypher, params = route_b2b_intent(intent, entities, req.vendor_id, limit=20)
    structured_data = None
    response_text = (
        "I can help with product recommendations, customer matching, allergen safety, "
        "and nutritional analysis. Try: 'Products free from peanuts' or 'Products for diabetic customers'."
    )

    if cypher and params:
        driver = _get_driver()
        rows = _run_cypher(driver, cypher, params)

        if rows:
            # Product-style rows (id, name, brand, score, ...)
            if any("id" in r and "name" in r for r in rows):
                products = [
                    {"id": str(r.get("id", "")), "name": str(r.get("name", "")), "brand": str(r.get("brand", "")),
                     "score": float(r.get("score", 0.9)), "calories": r.get("calories"), "protein_g": r.get("protein_g")}
                    for r in rows
                ]
                structured_data = {"products": products}
                count = len(products)
                if intent == "b2b_customers_with_condition":
                    response_text = f"Found {count} customer(s) matching your criteria."
                else:
                    response_text = f"Found {count} product(s)." + (f" Top: {products[0]['name']}" if products else "")
            # Customer-style rows
            elif any("customer_id" in r or "customer_name" in r for r in rows):
                customers = [
                    {"customer_id": str(r.get("customer_id", "")), "customer_name": str(r.get("customer_name", "")),
                     "email": str(r.get("email", ""))}
                    for r in rows
                ]
                structured_data = {"customers": customers}
                response_text = f"Found {len(customers)} customer(s)."

    report_data: list[dict[str, Any]] | None = None
    if structured_data:
        if "products" in structured_data:
            report_data = structured_data["products"]
        elif "customers" in structured_data:
            report_data = structured_data["customers"]

    add_message(session_id, "assistant", response_text)
    return ChatResponse(
        response=response_text,
        intent=intent,
        entities=entities,
        session_id=session_id,
        structured_data=structured_data,
        report_data=report_data,
    )


@router.post("/safety-check", response_model=SafetyCheckResponse)
async def safety_check(req: SafetyCheckRequest):
    """Product-customer allergen conflicts. Cross-reactivity when available."""
    start = time.time()
    driver = _get_driver()

    cypher, params = build_b2b_safety_check(
        vendor_id=req.vendor_id,
        product_ids=req.product_ids,
        customer_ids=req.customer_ids,
    )

    rows = _run_cypher(driver, cypher, params)
    conflicts = [
        SafetyConflictItem(
            product_id=str(r.get("product_id", "")),
            product_name=str(r.get("product_name", "")),
            customer_id=str(r.get("customer_id", "")),
            customer_name=str(r.get("customer_name", "")),
            conflict_allergen=str(r.get("conflict_allergen", "")),
            allergen_code=str(r.get("allergen_code", "")),
            customer_severity=str(r.get("customer_severity", "")),
        )
        for r in rows
    ]

    elapsed = (time.time() - start) * 1000
    summary_obj = {
        "total_conflicts": len(conflicts),
        "critical_count": sum(1 for c in conflicts if c.customer_severity in ("anaphylactic", "severe")),
        "affected_customers": len({c.customer_id for c in conflicts}),
        "affected_products": len({c.product_id for c in conflicts}),
    }
    n = summary_obj["total_conflicts"]
    crit = summary_obj["critical_count"]
    summary_str = f"{n} conflict(s) found" + (f", {crit} critical" if crit > 0 else "")
    return SafetyCheckResponse(
        conflicts=conflicts,
        cross_reactive=[],
        summary=summary_obj,
        summary_str=summary_str,
        retrieval_time_ms=elapsed,
    )
