from __future__ import annotations

from typing import Any


USDA_GROUPS: tuple[str, ...] = (
    "protein",
    "dairy",
    "vegetables",
    "fruits",
    "whole_grains",
)

# Deterministic relaxation order from Phase E plan.
RELAXATION_ORDER: tuple[str, ...] = (
    "whole_grains",
    "fruits",
    "vegetables",
    "dairy",
    "protein",
)


def aggregate_food_group_totals(candidates: list[dict[str, Any]]) -> dict[str, float]:
    """
    Aggregate food-group totals across selected recipe candidates.

    For Phase E minimal implementation, each group present in a candidate counts
    as +1.0 for that candidate.
    """
    totals = {g: 0.0 for g in USDA_GROUPS}
    for c in candidates:
        raw = c.get("food_groups")
        if not isinstance(raw, list):
            continue
        present = {str(g).strip().lower() for g in raw if isinstance(g, str)}
        for g in USDA_GROUPS:
            if g in present:
                totals[g] += 1.0
    return totals


def _safe_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def scale_targets(
    usda_guidelines: dict[str, Any] | None,
    *,
    calorie_target: float | None,
    expected_meals: int,
) -> dict[str, dict[str, float]]:
    """
    Build scaled per-set targets by food group.

    - Start from guideline target_default (daily)
    - Scale by calorie target / 2000
    - Scale to selected set size using expected_meals
    """
    scale = 1.0
    if calorie_target is not None and calorie_target > 0:
        scale = max(0.5, min(2.0, calorie_target / 2000.0))

    meals = max(1, expected_meals)
    groups_cfg = (usda_guidelines or {}).get("groups", {}) if isinstance(usda_guidelines, dict) else {}

    out: dict[str, dict[str, float]] = {}
    for g in USDA_GROUPS:
        cfg = groups_cfg.get(g, {}) if isinstance(groups_cfg, dict) else {}
        daily_target = _safe_float(cfg.get("target_default")) or 1.0
        soft_threshold = _safe_float(cfg.get("soft_threshold")) or 0.8
        # Convert daily target to the selected set target.
        # We assume 3 main meals/day in this phase.
        set_target = (daily_target / 3.0) * meals * scale
        out[g] = {
            "target": set_target,
            "soft_threshold": soft_threshold,
        }
    return out


def classify_status(actual: float, target: float, soft_threshold: float) -> str:
    low = target * soft_threshold
    high = target * 1.25
    if actual < low:
        return "below"
    if actual > high:
        return "above"
    return "adequate"


def audit_candidate_set(
    candidates: list[dict[str, Any]],
    *,
    usda_guidelines: dict[str, Any] | None,
    calorie_target: float | None,
    expected_meals: int,
) -> dict[str, Any]:
    totals = aggregate_food_group_totals(candidates)
    targets = scale_targets(
        usda_guidelines,
        calorie_target=calorie_target,
        expected_meals=expected_meals,
    )

    rows: list[dict[str, Any]] = []
    missing_groups: list[str] = []
    adequate_count = 0
    for g in USDA_GROUPS:
        actual = totals.get(g, 0.0)
        target = targets[g]["target"]
        soft = targets[g]["soft_threshold"]
        status = classify_status(actual, target, soft)
        if status == "below":
            missing_groups.append(g)
        else:
            adequate_count += 1
        rows.append(
            {
                "group": g,
                "actual": actual,
                "target": target,
                "status": status,
            }
        )

    return {
        "food_group_audit": rows,
        "missing_groups": missing_groups,
        "coverage_ratio": adequate_count / float(len(USDA_GROUPS)),
    }


def build_audit_warnings(missing_groups: list[str]) -> list[str]:
    if not missing_groups:
        return []

    ordered_missing = [g for g in RELAXATION_ORDER if g in set(missing_groups)]
    warnings = [
        "USDA coverage is partial; returning best-feasible candidates.",
        f"Missing food groups: {', '.join(missing_groups)}.",
    ]
    if ordered_missing:
        warnings.append(
            "Relax soft USDA goals in order: " + " -> ".join(ordered_missing) + "."
        )
    return warnings
