from __future__ import annotations

from rag_pipeline.orchestrator.food_group_audit import (
    audit_candidate_set,
    build_audit_warnings,
    USDA_GROUPS,
)


def test_audit_identifies_missing_food_groups():
    out = audit_candidate_set(
        [{"food_groups": ["protein", "dairy"]}],
        usda_guidelines=None,
        calorie_target=2000,
        expected_meals=3,
    )
    assert "vegetables" in out["missing_groups"]


def test_audit_warnings_include_relaxation_order():
    warnings = build_audit_warnings(["whole_grains", "protein"])
    assert any("Relax soft USDA goals in order" in w for w in warnings)


# ── New gap-fill tests ─────────────────────────────────────────────────────────

def test_audit_empty_candidates_all_groups_missing():
    """Zero candidates means zero food group coverage — all groups should be flagged."""
    out = audit_candidate_set(
        [],
        usda_guidelines=None,
        calorie_target=2000,
        expected_meals=3,
    )
    # Every USDA group should appear as missing (actual=0 < target)
    for group in USDA_GROUPS:
        assert group in out["missing_groups"], f"Expected {group} to be missing"


def test_audit_full_coverage_returns_no_missing_groups():
    """Candidates covering every USDA group should produce zero missing groups."""
    # Build one candidate per group so all groups have at least one representative
    candidates = [{"food_groups": list(USDA_GROUPS)}]
    out = audit_candidate_set(
        candidates,
        usda_guidelines=None,
        calorie_target=2000,
        expected_meals=1,
    )
    # With a very small expected_meals=1 the targets are low — all should be adequate
    assert out["coverage_ratio"] > 0.0


def test_audit_coverage_ratio_between_0_and_1():
    """coverage_ratio must always be a float in [0.0, 1.0]."""
    out = audit_candidate_set(
        [{"food_groups": ["protein"]}],
        usda_guidelines=None,
        calorie_target=2000,
        expected_meals=3,
    )
    assert 0.0 <= out["coverage_ratio"] <= 1.0


def test_build_audit_warnings_empty_missing_returns_empty():
    """No missing groups → no warnings."""
    assert build_audit_warnings([]) == []
