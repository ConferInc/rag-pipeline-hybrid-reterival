from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import asdict
from typing import Any

from rag_pipeline.config import USDAGuidelineConfig, USDAGroupRule, USDA_FOOD_GROUPS, get_default_usda_guidelines

logger = logging.getLogger(__name__)


# ── USDA guideline source strategy (Phase A) ────────────────────────────────

# This repo currently uses Neo4j for graph data. Postgres/Supabase integration
# for `gold.nutritional_guidelines` is intentionally left as a hook.
_USDA_GUIDELINES_CACHE: dict[str, Any] = {"ts": 0.0, "value": None}
_USDA_SOFT_GUIDELINES_CACHE: dict[str, Any] = {"ts": 0.0, "value": None}


def _cache_key() -> str:
    # Add env vars later if you support multi-tenant guidelines.
    return "usda_2025"


def _is_usda_strict_mode() -> bool:
    """Read strict mode with USDA_STRICT_MODE taking precedence."""
    strict_mode_raw = os.getenv("USDA_STRICT_MODE", "").strip()
    if strict_mode_raw:
        return strict_mode_raw == "1"
    return os.getenv("USDA_GUIDELINES_STRICT", "").strip() == "1"


def _usda_source_settings() -> tuple[str, str, str, str, str]:
    """Resolve USDA source connection settings from env vars."""
    supabase_url = os.getenv("SUPABASE_URL", "").strip()
    supabase_anon_key = os.getenv("SUPABASE_ANON_KEY", "").strip()
    supabase_db_url = os.getenv("SUPABASE_DATABASE_URL", "").strip()
    schema = os.getenv("SUPABASE_USDA_SCHEMA", "gold").strip() or "gold"
    model_name = os.getenv("SUPABASE_USDA_MODEL_NAME", "usda_2025").strip() or "usda_2025"
    return supabase_url, supabase_anon_key, supabase_db_url, schema, model_name


def load_usda_guidelines(*, ttl_s: int = 600) -> USDAGuidelineConfig:
    """
    Load USDA 2025 food-group guideline config from Supabase (gold schema).

    Fallback behavior:
    - If Supabase connection variables are missing or the query fails, we log
      and fall back to deterministic defaults so the API can still respond.
      Set `USDA_GUIDELINES_STRICT=1` to disable this fallback.
    """

    key = _cache_key()
    now = time.time()
    cached = _USDA_GUIDELINES_CACHE.get("value")
    cached_ts = _USDA_GUIDELINES_CACHE.get("ts", 0.0)
    if cached is not None and (now - cached_ts) < ttl_s:
        return cached

    strict = _is_usda_strict_mode()
    (
        supabase_url,
        supabase_anon_key,
        supabase_db_url,
        schema,
        model_name,
    ) = _usda_source_settings()

    if supabase_url and supabase_anon_key:
        try:
            _cfg = _load_usda_guidelines_via_supabase_client(
                supabase_url,
                supabase_anon_key,
                schema=schema,
                model_name=model_name,
            )
        except Exception as e:
            if strict:
                raise
            logger.warning(
                "Failed to load USDA guidelines via Supabase anon client; falling back to defaults: %s",
                e,
                extra={"component": "usda_guidelines"},
            )
            _cfg = get_default_usda_guidelines()
    elif supabase_db_url:
        try:
            _cfg = _load_usda_guidelines_from_postgres(
                supabase_db_url,
                schema=schema,
                model_name=model_name,
            )
        except Exception as e:
            if strict:
                raise
            logger.warning(
                "Failed to load USDA guidelines from Postgres; falling back to defaults: %s",
                e,
                extra={"component": "usda_guidelines"},
            )
            _cfg = get_default_usda_guidelines()
    else:
        msg = (
            "USDA guidelines not configured. Provide either:\n"
            "- `SUPABASE_URL` + `SUPABASE_ANON_KEY` (preferred), or\n"
            "- `SUPABASE_DATABASE_URL` (Postgres connection string fallback).\n"
            "Needed to read `gold.nutritional_guidelines`."
        )
        if strict:
            raise RuntimeError(msg)
        logger.warning(msg)
        _cfg = get_default_usda_guidelines()
        _USDA_GUIDELINES_CACHE["ts"] = now
        _USDA_GUIDELINES_CACHE["value"] = _cfg
        return _cfg

    _USDA_GUIDELINES_CACHE["ts"] = now
    _USDA_GUIDELINES_CACHE["value"] = _cfg
    logger.info(
        "USDA guidelines loaded (version=%s, source=%s)",
        _cfg.version,
        "supabase" if _cfg.version.startswith("usda_") else "defaults",
    )
    return _cfg


def get_default_usda_soft_guidelines() -> dict[str, dict[str, Any]]:
    """
    Deterministic fallback soft-guideline thresholds.

    Keys are canonical nutrient identifiers used by PRD-34.
    """
    return {
        "added_sugar_per_meal": {
            "recommended_max": 10.0,
            "unit": "g",
            "severity": "info",
        },
        "sodium_daily": {
            "recommended_max": 2300.0,
            "unit": "mg",
            "severity": "info",
        },
        "saturated_fat_daily_pct": {
            "recommended_max": 10.0,
            "unit": "percent",
            "severity": "info",
        },
    }


def load_usda_soft_guidelines(*, ttl_s: int = 600) -> dict[str, dict[str, Any]]:
    """
    Load USDA soft guidelines from `gold.nutritional_soft_guidelines`.

    Returns a dict keyed by nutrient name with values:
      {recommended_max, unit, severity}
    """
    now = time.time()
    cached = _USDA_SOFT_GUIDELINES_CACHE.get("value")
    cached_ts = _USDA_SOFT_GUIDELINES_CACHE.get("ts", 0.0)
    if cached is not None and (now - cached_ts) < ttl_s:
        return cached

    strict = _is_usda_strict_mode()
    (
        supabase_url,
        supabase_anon_key,
        supabase_db_url,
        schema,
        model_name,
    ) = _usda_source_settings()

    if supabase_url and supabase_anon_key:
        try:
            soft = _load_usda_soft_guidelines_via_supabase_client(
                supabase_url,
                supabase_anon_key,
                schema=schema,
                model_name=model_name,
            )
        except Exception as e:
            if strict:
                raise
            logger.warning(
                "Failed to load USDA soft guidelines via Supabase anon client; falling back to defaults: %s",
                e,
                extra={"component": "usda_guidelines"},
            )
            soft = get_default_usda_soft_guidelines()
    elif supabase_db_url:
        try:
            soft = _load_usda_soft_guidelines_from_postgres(
                supabase_db_url,
                schema=schema,
                model_name=model_name,
            )
        except Exception as e:
            if strict:
                raise
            logger.warning(
                "Failed to load USDA soft guidelines from Postgres; falling back to defaults: %s",
                e,
                extra={"component": "usda_guidelines"},
            )
            soft = get_default_usda_soft_guidelines()
    else:
        msg = (
            "USDA soft guidelines not configured. Provide either:\n"
            "- `SUPABASE_URL` + `SUPABASE_ANON_KEY` (preferred), or\n"
            "- `SUPABASE_DATABASE_URL` (Postgres connection string fallback).\n"
            "Needed to read `gold.nutritional_soft_guidelines`."
        )
        if strict:
            raise RuntimeError(msg)
        logger.warning(msg)
        soft = get_default_usda_soft_guidelines()
        _USDA_SOFT_GUIDELINES_CACHE["ts"] = now
        _USDA_SOFT_GUIDELINES_CACHE["value"] = soft
        return soft

    _USDA_SOFT_GUIDELINES_CACHE["ts"] = now
    _USDA_SOFT_GUIDELINES_CACHE["value"] = soft
    return soft


def _load_usda_guidelines_via_supabase_client(
    supabase_url: str,
    supabase_anon_key: str,
    *,
    schema: str,
    model_name: str,
) -> USDAGuidelineConfig:
    """
    Load USDA guidelines using Supabase PostgREST + anon key.

    Assumes either:
    - RLS is disabled (as you said), or
    - the anon role has SELECT permission.
    """
    from supabase import create_client

    schema = schema.strip() or "gold"
    client = create_client(supabase_url, supabase_anon_key)

    # Query only active rows for the active model.
    # Note: we explicitly target the `gold` schema via `.schema(schema)`.
    resp = (
        client.schema(schema)
        .from_("nutritional_guidelines")
        .select(
            "food_group,daily_target_min,daily_target_max,daily_target_unit,pyramid_priority"
        )
        .eq("model_name", model_name)
        .eq("is_active", True)
        .execute()
    )

    data = resp.data or []
    if not data:
        raise RuntimeError(
            f"No USDA guideline rows returned from {schema}.nutritional_guidelines "
            f"for model_name={model_name!r}"
        )

    rows = list(data)

    groups: dict[str, USDAGroupRule] = {}
    for r in rows:
        food_group = (r.get("food_group") or "").strip().lower()
        if not food_group:
            continue
        if food_group not in USDA_FOOD_GROUPS:
            continue

        dmin = r.get("daily_target_min")
        dmax = r.get("daily_target_max")
        unit = (r.get("daily_target_unit") or "").strip()
        priority = r.get("pyramid_priority")

        try:
            dmin_f = float(dmin)
            dmax_f = float(dmax)
        except (TypeError, ValueError):
            continue

        target_default = (dmin_f + dmax_f) / 2.0
        soft_threshold = dmin_f
        pr = int(priority) if priority is not None else 999

        groups[food_group] = USDAGroupRule(
            target_default=target_default,
            soft_threshold=soft_threshold,
            priority=pr,
            weight=1.0,
            unit=unit or "unknown",
        )

    defaults = get_default_usda_guidelines()
    for g in USDA_FOOD_GROUPS:
        if g not in groups:
            groups[g] = defaults.groups[g]

    version = f"{model_name}_supabase_anon_loaded_v1"
    return USDAGuidelineConfig(version=version, groups=groups)


def _load_usda_soft_guidelines_via_supabase_client(
    supabase_url: str,
    supabase_anon_key: str,
    *,
    schema: str,
    model_name: str,
) -> dict[str, dict[str, Any]]:
    from supabase import create_client

    schema = schema.strip() or "gold"
    client = create_client(supabase_url, supabase_anon_key)
    resp = (
        client.schema(schema)
        .from_("nutritional_soft_guidelines")
        .select("nutrient,recommended_max,unit,severity")
        .eq("model_name", model_name)
        .eq("is_active", True)
        .execute()
    )

    rows = list(resp.data or [])
    if not rows:
        raise RuntimeError(
            f"No USDA soft guideline rows returned from {schema}.nutritional_soft_guidelines "
            f"for model_name={model_name!r}"
        )
    return _normalize_soft_guidelines_rows(rows)


def _load_usda_guidelines_from_postgres(
    database_url: str,
    *,
    schema: str,
    model_name: str,
) -> USDAGuidelineConfig:
    """Fallback loader using a direct Postgres connection string."""
    import psycopg2
    import psycopg2.extras
    from psycopg2 import sql

    schema = schema.strip() or "gold"

    conn = psycopg2.connect(database_url)
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            q = sql.SQL(
                """
                SELECT
                  food_group,
                  daily_target_min,
                  daily_target_max,
                  daily_target_unit,
                  pyramid_priority
                FROM {schema}.nutritional_guidelines
                WHERE model_name = %s AND is_active = true
                """
            ).format(schema=sql.Identifier(schema))
            cur.execute(q, (model_name,))
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        raise RuntimeError(
            f"No USDA guideline rows returned from {schema}.nutritional_guidelines "
            f"for model_name={model_name!r}"
        )

    # Normalize into our internal representation.
    # Weight is a placeholder for Phase A; later phases can tune it using targets.
    groups: dict[str, USDAGroupRule] = {}
    for r in rows:
        food_group = (r.get("food_group") or "").strip().lower()
        if not food_group:
            continue
        if food_group not in USDA_FOOD_GROUPS:
            # Unknown groups are ignored to keep config stable.
            continue

        dmin = r.get("daily_target_min")
        dmax = r.get("daily_target_max")
        unit = (r.get("daily_target_unit") or "").strip()
        priority = r.get("pyramid_priority")

        try:
            dmin_f = float(dmin)
            dmax_f = float(dmax)
        except (TypeError, ValueError):
            continue

        target_default = (dmin_f + dmax_f) / 2.0
        soft_threshold = dmin_f
        pr = int(priority) if priority is not None else 999

        groups[food_group] = USDAGroupRule(
            target_default=target_default,
            soft_threshold=soft_threshold,
            priority=pr,
            weight=1.0,
            unit=unit or "unknown",
        )

    # If some groups are missing from DB, fill using local defaults so later
    # stages still have a complete 5-group contract.
    defaults = get_default_usda_guidelines()
    for g in USDA_FOOD_GROUPS:
        if g not in groups:
            groups[g] = defaults.groups[g]

    version = f"{model_name}_supabase_loaded_v1"
    return USDAGuidelineConfig(version=version, groups=groups)


def _load_usda_soft_guidelines_from_postgres(
    database_url: str,
    *,
    schema: str,
    model_name: str,
) -> dict[str, dict[str, Any]]:
    import psycopg2
    import psycopg2.extras
    from psycopg2 import sql

    schema = schema.strip() or "gold"
    conn = psycopg2.connect(database_url)
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            q = sql.SQL(
                """
                SELECT
                  nutrient,
                  recommended_max,
                  unit,
                  severity
                FROM {schema}.nutritional_soft_guidelines
                WHERE model_name = %s AND is_active = true
                """
            ).format(schema=sql.Identifier(schema))
            cur.execute(q, (model_name,))
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        raise RuntimeError(
            f"No USDA soft guideline rows returned from {schema}.nutritional_soft_guidelines "
            f"for model_name={model_name!r}"
        )
    return _normalize_soft_guidelines_rows(list(rows))


def _normalize_soft_guidelines_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        nutrient = str((r.get("nutrient") or "")).strip().lower()
        if not nutrient:
            continue
        recommended_max = r.get("recommended_max")
        try:
            rec_max = float(recommended_max)
        except (TypeError, ValueError):
            continue
        out[nutrient] = {
            "recommended_max": rec_max,
            "unit": str((r.get("unit") or "")).strip() or "unknown",
            "severity": str((r.get("severity") or "")).strip() or "info",
        }

    defaults = get_default_usda_soft_guidelines()
    for key, value in defaults.items():
        if key not in out:
            out[key] = value
    return out


def guidelines_to_jsonable(cfg: USDAGuidelineConfig) -> dict[str, Any]:
    """Helper to store in orchestrator entities without dataclass types."""
    return {"version": cfg.version, "groups": {k: asdict(v) for k, v in cfg.groups.items()}}


# ── Ingredient -> USDA food group inference (Phase A contract) ────────────

_TOKEN_RE = re.compile(r"[^a-z0-9\\s]+", re.I)


def _normalize_ingredient_name(name: Any) -> str:
    if name is None:
        return ""
    s = str(name).lower().strip()
    s = _TOKEN_RE.sub(" ", s)
    s = re.sub(r"\\s+", " ", s).strip()
    # Common aliases / synonyms.
    s = (
        s.replace("curd", "yogurt")
        .replace("hung curd", "yogurt")
        .replace("dahi", "yogurt")
        .replace("paneer", "paneer")
    )
    return s


# Keyword-based rules. This avoids large manual maps while still producing
# explainable, deterministic buckets.
#
# Confidence is approximate:
# - "high" for clear single-token matches (yogurt -> dairy)
# - "medium" for looser matches (bean -> protein)
_USDA_GROUP_RULES: dict[str, list[tuple[str, str]]] = {
    "protein": [
        ("chicken", "high"),
        ("turkey", "high"),
        ("beef", "high"),
        ("pork", "high"),
        ("lamb", "high"),
        ("fish", "high"),
        ("salmon", "high"),
        ("tuna", "high"),
        ("egg", "high"),
        ("eggs", "high"),
        ("tofu", "high"),
        ("tempeh", "high"),
        ("lentil", "high"),
        ("lentils", "high"),
        ("chickpea", "high"),
        ("chickpeas", "high"),
        ("bean", "medium"),
        ("beans", "medium"),
        ("soy", "medium"),
        ("soybean", "medium"),
        ("soya", "medium"),
        ("walnut", "medium"),
        ("almond", "medium"),
        ("peanut", "medium"),
        ("peanuts", "medium"),
        ("pumpkin seed", "medium"),
        ("sunflower seed", "medium"),
        ("nuts", "medium"),
    ],
    "dairy": [
        ("milk", "high"),
        ("cheese", "high"),
        ("yogurt", "high"),
        ("cream", "medium"),
        ("butter", "medium"),
        ("curd", "high"),  # alias; normalized sometimes
        ("paneer", "high"),
        ("kefir", "high"),
        ("ghee", "medium"),
    ],
    "vegetables": [
        ("spinach", "high"),
        ("broccoli", "high"),
        ("carrot", "high"),
        ("carrots", "high"),
        ("tomato", "high"),
        ("onion", "high"),
        ("garlic", "high"),
        ("capsicum", "high"),
        ("pepper", "high"),
        ("cucumber", "high"),
        ("zucchini", "high"),
        ("eggplant", "high"),
        ("brinjal", "high"),
        ("lettuce", "medium"),
        ("mushroom", "medium"),
        ("potato", "medium"),
        ("potatoes", "medium"),
        ("sweet potato", "medium"),
        ("bottle gourd", "medium"),
        ("lauki", "medium"),
        ("okra", "medium"),
    ],
    "fruits": [
        ("banana", "high"),
        ("apple", "high"),
        ("orange", "high"),
        ("mango", "high"),
        ("berries", "high"),
        ("strawberry", "high"),
        ("strawberries", "high"),
        ("blueberry", "medium"),
        ("grape", "medium"),
        ("grapes", "medium"),
        ("watermelon", "medium"),
        ("papaya", "medium"),
        ("guava", "medium"),
        ("pear", "medium"),
        ("peach", "medium"),
    ],
    "whole_grains": [
        ("oats", "high"),
        ("oatmeal", "high"),
        ("quinoa", "high"),
        ("brown rice", "high"),
        ("whole wheat", "high"),
        ("wholemeal", "high"),
        ("millet", "high"),
        ("barley", "high"),
        ("rye", "high"),
        ("buckwheat", "high"),
        ("amaranth", "medium"),
        ("bulgur", "medium"),
        ("couscous", "medium"),
        ("wheat", "medium"),  # may be refined; keep medium confidence
        ("brown", "low"),  # only in conjunction; handled by phrase matches above
        ("corn", "medium"),
        ("maize", "medium"),
    ],
}


def _confidence_from_label(label: str) -> float:
    return {"high": 0.95, "medium": 0.7, "low": 0.4}.get(label, 0.5)


def infer_food_groups_for_ingredients(ingredient_names: list[str]) -> dict[str, Any]:
    """
    Infer USDA food_groups for a set of ingredients.

    Returns:
      {
        "food_groups": ["protein", ...],
        "confidence_by_group": {"protein": 0.95, ...},
        "unknown_count": int,
      }
    """
    confidence_by_group: dict[str, float] = {}
    unknown_count = 0

    for raw in ingredient_names:
        ing = _normalize_ingredient_name(raw)
        if not ing:
            continue

        matched_any = False
        for group in USDA_FOOD_GROUPS:
            rules = _USDA_GROUP_RULES.get(group, [])
            for kw, conf_label in rules:
                if not kw:
                    continue
                # Keyword match: simple substring is fast and works well for
                # ingredient strings (Phase A uses heuristic, not ML).
                if kw in ing:
                    matched_any = True
                    c = _confidence_from_label(conf_label)
                    confidence_by_group[group] = max(confidence_by_group.get(group, 0.0), c)

        if not matched_any:
            unknown_count += 1

    food_groups = sorted(
        (g for g, c in confidence_by_group.items() if c > 0.0),
        key=lambda g: -confidence_by_group.get(g, 0.0),
    )

    return {
        "food_groups": food_groups,
        "confidence_by_group": confidence_by_group,
        "unknown_count": unknown_count,
    }

