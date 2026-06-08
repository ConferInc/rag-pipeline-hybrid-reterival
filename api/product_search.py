"""
PRD-40: product search + safety-annotation orchestrator for `/search/products`.

Three modes (driven by the request, not separate endpoints):
  - search       : query present → semantic retrieval + allergen annotation
  - annotate-only: seed_product_ids present, annotate_only=True → just annotate
  - hybrid       : query + seed_product_ids → annotate seeds AND supplement semantically

Stock-blind by design: this returns *similarity* + *allergen safety* only. The
Express backend re-validates availability against Postgres `gold.store_products`
and does the final ranking. Existing `/recommend/products` and
`/recommend/alternatives` contracts are untouched.

The candidate→item assembly is split into the PURE helpers below so it can be
unit-tested without a Neo4j driver (codebase convention: pure-logic tests only).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .product_recommendation import _filter_allergen_unsafe_product_ids
from rag_pipeline.retrieval.product_hybrid import semantic_search_products

if TYPE_CHECKING:
    from neo4j import Driver
    from rag_pipeline.embeddings.base import QueryEmbedder

logger = logging.getLogger(__name__)


# ── Pure helpers (unit-tested directly) ──────────────────────────────────────

def resolve_member_allergen_map(
    household_members: list[dict[str, Any]] | None,
) -> dict[str, list[str]]:
    """Build { member_id: [allergen tokens] } from a household_members payload.

    Accepts `allergen_ids`, `allergens`, or `allergen_codes` as the token list
    (any combination), so callers can pass IDs, names, or codes. PURE.
    """
    out: dict[str, list[str]] = {}
    for m in household_members or []:
        if not isinstance(m, dict):
            continue
        mid = m.get("id") or m.get("customer_id") or m.get("member_id")
        if not mid:
            continue
        tokens: list[str] = []
        for key in ("allergen_ids", "allergens", "allergen_codes"):
            vals = m.get(key)
            if vals:
                tokens.extend(str(v) for v in vals if v)
        out[str(mid)] = tokens
    return out


def assemble_product_items(
    all_ids: list[str],
    seed_ids: list[str],
    semantic_results: list[dict[str, Any]],
    unsafe_map: dict[str, dict[str, list[str]]],
    member_allergen_map: dict[str, list[str]],
) -> list[dict[str, Any]]:
    """Build the annotated product items. PURE — no driver/network.

    - safety: "warning" if the product hit any household allergen, else "safe"
    - affected_members: members whose allergen tokens intersect the matched
      allergen names/codes for that product
    - match_source: seed | semantic | both
    """
    seed_set = {str(s) for s in seed_ids}
    sem_by_id = {str(r["product_id"]): r for r in semantic_results if r.get("product_id")}

    # Pre-lower member tokens once.
    member_tokens_lower = {
        mid: {str(t).lower() for t in toks if t}
        for mid, toks in (member_allergen_map or {}).items()
    }

    items: list[dict[str, Any]] = []
    for pid in all_ids:
        pid = str(pid)
        unsafe = unsafe_map.get(pid)

        affected: list[str] = []
        matching_allergens: list[str] = []
        if unsafe:
            matching_allergens = list(unsafe.get("matching_allergens", []))
            matched_tokens = {
                str(x).lower()
                for x in (unsafe.get("allergen_codes", []) + unsafe.get("matching_allergens", []))
                if x
            }
            for mid, toks in member_tokens_lower.items():
                if toks & matched_tokens:
                    affected.append(mid)

        sem = sem_by_id.get(pid)
        in_seed = pid in seed_set
        if in_seed and sem:
            match_source = "both"
        elif in_seed:
            match_source = "seed"
        else:
            match_source = "semantic"

        items.append({
            "product_id": pid,
            "semantic_score": float(sem["score"]) if sem and sem.get("score") is not None else None,
            "safety": "warning" if unsafe else "safe",
            "matching_allergens": matching_allergens,
            "affected_members": affected,
            "match_source": match_source,
        })
    return items


def _dedupe_preserve_order(ids: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for i in ids:
        s = str(i)
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


# ── Orchestrator (driver-touching) ───────────────────────────────────────────

def run_search_products(
    driver: "Driver",
    *,
    query: str | None = None,
    seed_product_ids: list[str] | None = None,
    customer_allergens: list[str] | None = None,
    household_members: list[dict[str, Any]] | None = None,
    household_id: str | None = None,
    vendor_ids: list[str] | None = None,
    category_ids: list[str] | None = None,
    diet_ids: list[str] | None = None,
    annotate_only: bool = False,
    limit: int = 30,
    exclude_ids: list[str] | None = None,
    embedder: "QueryEmbedder | None" = None,
    database: str | None = None,
) -> dict[str, Any]:
    """Run product search + safety annotation. Returns {products, query_interpretation}."""
    seed_ids = _dedupe_preserve_order(list(seed_product_ids or []))

    # Allergen union (hard safety filter) + per-member map (affected attribution).
    allergen_union: list[str] = [a for a in (customer_allergens or []) if a]
    member_allergen_map = resolve_member_allergen_map(household_members)

    # Best-effort household enrichment when only household_id is given.
    if not member_allergen_map and household_id:
        try:
            from rag_pipeline.profile.household_profile import (
                aggregate_profile,
                fetch_household_profile,
            )

            member_profiles, member_meta = fetch_household_profile(driver, household_id, database)
            if member_profiles:
                agg = aggregate_profile(member_profiles)
                allergen_union = _dedupe_preserve_order(allergen_union + list(agg.get("allergens", [])))
                for meta, prof in zip(member_meta, member_profiles):
                    mid = meta.get("customer_id")
                    if mid:
                        member_allergen_map[str(mid)] = [a for a in (prof.get("allergens") or []) if a]
        except Exception as e:  # profile fetch is best-effort
            logger.warning("run_search_products household enrichment failed: %s", e)

    # Semantic supplement (skipped for annotate-only or when no query/embedder).
    semantic_results: list[dict[str, Any]] = []
    if query and query.strip() and not annotate_only:
        exclude = set(str(x) for x in (exclude_ids or [])) | set(seed_ids)
        remaining = max(1, limit - len(seed_ids))
        semantic_results = semantic_search_products(
            driver,
            embedder,
            query,
            exclude_ids=exclude,
            limit=remaining,
            filters={"vendor_ids": vendor_ids, "category_ids": category_ids, "diet_ids": diet_ids},
            database=database,
        )

    semantic_ids = [r["product_id"] for r in semantic_results]
    all_ids = _dedupe_preserve_order(seed_ids + semantic_ids)
    if not all_ids:
        return {"products": [], "query_interpretation": None}

    # Single allergen-graph traversal for the full candidate set.
    unsafe_map = _filter_allergen_unsafe_product_ids(driver, all_ids, allergen_union, database)

    items = assemble_product_items(all_ids, seed_ids, semantic_results, unsafe_map, member_allergen_map)
    return {"products": items, "query_interpretation": None}
