"""
Phase 1: Household-aware profile layer.

Fetches and aggregates customer profiles for household recommendations.
Does NOT modify the current pipeline — add new calls from handlers when ready.

Neo4j schema assumptions (from user):
  - Household: household_name, household_type (id for lookup)
  - B2C_Customer: household_id, role (primary_adult, child, dependent)
  - B2C_Customer relationships: FOLLOWS_DIET, IS_ALLERGIC, HAS_CONDITION, HAS_PROFILE, HAS_MEAL_LOG
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from neo4j import Driver

logger = logging.getLogger(__name__)

# Profile keys matching fetch_customer_profile output (api/app.py)
_PROFILE_KEYS = [
    "display_name",
    "diets",
    "allergens",
    "health_conditions",
    "health_goal",
    "activity_level",
    "recent_recipes",
]


def _empty_profile() -> dict[str, Any]:
    """Return an empty profile dict with the expected shape."""
    return {
        "display_name": None,
        "diets": [],
        "allergens": [],
        "health_conditions": [],
        "health_goal": None,
        "activity_level": None,
        "recent_recipes": [],
    }


def _record_to_profile(record: dict[str, Any]) -> dict[str, Any]:
    """Convert a Neo4j record row to a single-member profile dict."""
    name = record.get("display_name")
    if isinstance(name, str) and name.strip():
        name = name.strip()
    else:
        name = None
    diets_raw = record.get("diets") or []
    diets_clean = [d for d in diets_raw if d and isinstance(d, str)]
    allergens_raw = record.get("allergens") or []
    allergens_clean = [x for x in allergens_raw if x and isinstance(x, str)]
    hc_raw = record.get("health_conditions") or []
    hc_clean = [x for x in hc_raw if x and isinstance(x, str)]
    rr_raw = record.get("recent_recipes") or []
    rr_clean = [x for x in rr_raw if x and isinstance(x, str)]
    return {
        "display_name": name,
        "diets": diets_clean,
        "allergens": allergens_clean,
        "health_conditions": hc_clean,
        "health_goal": record.get("health_goal"),
        "activity_level": record.get("activity_level"),
        "recent_recipes": rr_clean,
    }


def _fetch_single_customer_profile(
    driver: Driver,
    customer_id: str,
    database: str | None = None,
) -> dict[str, Any]:
    """
    Fetch profile for a single B2C_Customer. Same shape as api.app.fetch_customer_profile.

    Kept internal to avoid modifying api.app. Used when member_id is specified
    or as fallback for self.
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
      collect(DISTINCT dp.name) AS diets,
      collect(DISTINCT a.name) AS allergens,
      collect(DISTINCT hc.name) AS health_conditions,
      hp.health_goal AS health_goal,
      hp.activity_level AS activity_level,
      collect(DISTINCT r.title) AS recent_recipes
    """
    try:
        with driver.session(database=database) as session:
            record = session.run(cypher, customer_id=customer_id).single()
            if not record:
                logger.debug("_fetch_single_customer_profile: no record for customer_id=%s", customer_id)
                return _empty_profile()
            return _record_to_profile(dict(record))
    except Exception as e:
        logger.warning("_fetch_single_customer_profile failed: %s", e)
        return _empty_profile()


# ── Profile aggregation rules ──────────────────────────────────────────────────
#   Allergens:      union   — avoid anything ANY member is allergic to
#   Diets:          intersection — only recipes that meet ALL members' diets
#   Health conditions: union — exclude anything contraindicated for ANY member
#   Recent recipes: union   — for variety / exclude recent across household
#   Health goal / activity: from primary_adult or first member (soft signals)
# ──────────────────────────────────────────────────────────────────────────────


def aggregate_profile(
    member_profiles: list[dict[str, Any]],
    *,
    member_roles: list[str] | None = None,
) -> dict[str, Any]:
    """
    Aggregate multiple member profiles into a single household profile for recommendations.

    Rules:
      - allergens: union (exclude any allergen any member has)
      - diets: intersection (recipes must satisfy all members' diets; empty = no restriction)
      - health_conditions: union (exclude anything bad for any member)
      - recent_recipes: union (exclude recently eaten by any member)
      - health_goal, activity_level: from first primary_adult, else first member
      - display_name: None (aggregate has no single name)

    Args:
        member_profiles: List of profile dicts (each with diets, allergens, health_conditions, etc.)
        member_roles: Optional list of roles in same order as member_profiles
                      (primary_adult, child, dependent) — used to pick health_goal/activity_level
    """
    if not member_profiles:
        return _empty_profile()

    allergens: set[str] = set()
    diets_per_member: list[set[str]] = []
    health_conditions: set[str] = set()
    recent_recipes: set[str] = set()
    health_goal = None
    activity_level = None
    roles = member_roles or []

    primary_goal = None
    primary_activity = None
    fallback_goal = None
    fallback_activity = None

    for i, p in enumerate(member_profiles):
        a = p.get("allergens") or []
        allergens.update(x for x in a if x and isinstance(x, str))
        d = p.get("diets") or []
        diets_per_member.append({x for x in d if x and isinstance(x, str)})
        hc = p.get("health_conditions") or []
        health_conditions.update(x for x in hc if x and isinstance(x, str))
        rr = p.get("recent_recipes") or []
        recent_recipes.update(x for x in rr if x and isinstance(x, str))
        role = roles[i] if i < len(roles) else None
        if role == "primary_adult" and primary_goal is None and primary_activity is None:
            primary_goal = p.get("health_goal")
            primary_activity = p.get("activity_level")
        if fallback_goal is None and fallback_activity is None and (p.get("health_goal") or p.get("activity_level")):
            fallback_goal = p.get("health_goal")
            fallback_activity = p.get("activity_level")

    health_goal = primary_goal or fallback_goal
    activity_level = primary_activity or fallback_activity

    # Intersection of diets: recipes must comply with all members
    if diets_per_member:
        diet_intersection = diets_per_member[0].copy()
        for s in diets_per_member[1:]:
            diet_intersection &= s
        diets_list = list(diet_intersection)
    else:
        diets_list = []

    return {
        "display_name": None,
        "diets": diets_list,
        "allergens": list(allergens),
        "health_conditions": list(health_conditions),
        "health_goal": health_goal,
        "activity_level": activity_level,
        "recent_recipes": list(recent_recipes),
    }


def get_household_id_for_customer(
    driver: Driver,
    customer_id: str,
    database: str | None = None,
) -> str | None:
    """
    Get household_id for a customer from B2C_Customer.household_id.

    Returns None if customer not found or household_id is missing.
    """
    cypher = """
    MATCH (c:B2C_Customer)
    WHERE c.id = $customer_id OR elementId(c) = $customer_id
    RETURN c.household_id AS household_id
    """
    try:
        with driver.session(database=database) as session:
            record = session.run(cypher, customer_id=customer_id).single()
            if not record:
                return None
            hh_id = record.get("household_id")
            return str(hh_id).strip() if hh_id else None
    except Exception as e:
        logger.warning("get_household_id_for_customer failed: %s", e)
        return None


def fetch_household_profile(
    driver: Driver,
    household_id: str,
    database: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Fetch profiles for all B2C_Customers in a household.

    Uses B2C_Customer.household_id property to find members. If your schema uses
    (Household)-[:HAS_MEMBER]->(B2C_Customer) or (B2C_Customer)-[:BELONGS_TO_HOUSEHOLD]->(Household),
    you may need to adjust the Cypher.

    Returns:
        (member_profiles, member_meta): list of profile dicts and list of
        {customer_id, role, display_name} per member.
    """
    # Option A: B2C_Customer has household_id property (user's schema)
    cypher = """
    MATCH (c:B2C_Customer)
    WHERE c.household_id = $household_id
    OPTIONAL MATCH (c)-[:FOLLOWS_DIET]->(dp:Dietary_Preferences)
    OPTIONAL MATCH (c)-[:IS_ALLERGIC]->(a:Allergens)
    OPTIONAL MATCH (c)-[:HAS_CONDITION]->(hc:B2C_Customer_Health_Conditions)
    OPTIONAL MATCH (c)-[:HAS_PROFILE]->(hp:B2C_Customer_Health_Profiles)
    OPTIONAL MATCH (c)-[:HAS_MEAL_LOG]->(ml:MealLog)
                   -[:CONTAINS_ITEM]->(mli:MealLogItem)
                   -[:OF_RECIPE]->(r:Recipe)
    WHERE (ml IS NULL OR ml.log_date >= date() - duration({days: 14}))
    RETURN
      c.id AS customer_id,
      c.role AS role,
      coalesce(c.display_name, c.full_name, c.name) AS display_name,
      collect(DISTINCT dp.name) AS diets,
      collect(DISTINCT a.name) AS allergens,
      collect(DISTINCT hc.name) AS health_conditions,
      hp.health_goal AS health_goal,
      hp.activity_level AS activity_level,
      collect(DISTINCT r.title) AS recent_recipes
    """
    try:
        with driver.session(database=database) as session:
            result = session.run(cypher, household_id=household_id)
            member_profiles: list[dict[str, Any]] = []
            member_meta: list[dict[str, Any]] = []
            for record in result:
                r = dict(record)
                member_meta.append({
                    "customer_id": r.get("customer_id"),
                    "role": r.get("role"),
                    "display_name": r.get("display_name"),
                })
                member_profiles.append(_record_to_profile(r))
            return (member_profiles, member_meta)
    except Exception as e:
        logger.warning("fetch_household_profile failed: %s", e)
        return ([], [])


def resolve_profile_for_role(
    driver: Driver,
    household_id: str,
    role_type: str,
    database: str | None = None,
) -> dict[str, Any]:
    """
    Resolve profile for household members with the given role_type.

    When multiple members share the role (e.g. two children), their profiles
    are aggregated (union allergens, intersection diets, etc.).

    Args:
        driver: Neo4j driver
        household_id: Household ID
        role_type: "primary_adult" | "child" | "dependent" — matches B2C_Customer.role
        database: Neo4j database name

    Returns:
        Profile dict with same shape as fetch_customer_profile.
    """
    member_profiles, member_meta = fetch_household_profile(driver, household_id, database)
    role_lower = (role_type or "").strip().lower()
    if not role_lower:
        return _empty_profile()

    filtered_profiles: list[dict[str, Any]] = []
    filtered_meta: list[dict[str, Any]] = []
    for i, meta in enumerate(member_meta):
        m_role = (meta.get("role") or "").strip().lower()
        if m_role == role_lower:
            filtered_profiles.append(member_profiles[i])
            filtered_meta.append(meta)

    if not filtered_profiles:
        logger.debug(
            "resolve_profile_for_role: no members with role=%s in household_id=%s",
            role_type,
            household_id,
        )
        return _empty_profile()

    return aggregate_profile(filtered_profiles, member_roles=[m.get("role") for m in filtered_meta])


def resolve_profile_for_recommendation(
    driver: Driver,
    customer_id: str,
    *,
    household_id: str | None = None,
    member_id: str | None = None,
    family_scope: str | None = None,
    target_member_role: str | None = None,
    database: str | None = None,
) -> dict[str, Any]:
    """
    Resolve which profile to use for recommendations based on context.

    Logic:
      - member_id (customer_id) provided → use that member's profile (single-customer)
      - target_member_role set → use aggregated profile for members with that role
      - family_scope in ("family", "everyone", "all") → use aggregated household profile
      - family_scope "self" or otherwise → use logged-in customer's profile

    Args:
        driver: Neo4j driver
        customer_id: Logged-in customer (b2c_customer_id); used when no override
        household_id: Required for family_scope / target_member_role; looked up from customer if omitted
        member_id: When set, use this member's profile (b2c_customer_id — same ID type as customer_id)
        family_scope: "family", "everyone", "all" → household aggregate; "self" → customer
        target_member_role: "primary_adult" | "child" | "dependent" → profile for members with that role
        database: Neo4j database name

    Returns:
        Profile dict with same shape as fetch_customer_profile.
    """
    _family_values = frozenset(("family", "everyone", "all"))
    scope = (family_scope or "").strip().lower()

    # Explicit self
    if scope == "self":
        return _fetch_single_customer_profile(driver, customer_id, database)

    # Specific member by customer_id
    if member_id:
        return _fetch_single_customer_profile(driver, member_id, database)

    # Target member by role (primary_adult, child, dependent)
    if target_member_role:
        hh_id = household_id or get_household_id_for_customer(driver, customer_id, database)
        if hh_id:
            return resolve_profile_for_role(driver, hh_id, target_member_role, database)
        logger.debug(
            "resolve_profile_for_recommendation: no household_id for target_member_role=%s, falling back to customer",
            target_member_role,
        )
        return _fetch_single_customer_profile(driver, customer_id, database)

    # Family-wide: aggregate household profile
    if scope in _family_values:
        hh_id = household_id or get_household_id_for_customer(driver, customer_id, database)
        if not hh_id:
            logger.debug(
                "resolve_profile_for_recommendation: no household_id for family_scope, falling back to customer",
            )
            return _fetch_single_customer_profile(driver, customer_id, database)
        member_profiles, member_meta = fetch_household_profile(driver, hh_id, database)
        if not member_profiles:
            logger.debug(
                "resolve_profile_for_recommendation: no household members for household_id=%s, falling back to customer",
                household_id,
            )
            return _fetch_single_customer_profile(driver, customer_id, database)
        roles = [m.get("role") for m in member_meta]
        return aggregate_profile(member_profiles, member_roles=roles)

    # Default: logged-in customer
    return _fetch_single_customer_profile(driver, customer_id, database)
