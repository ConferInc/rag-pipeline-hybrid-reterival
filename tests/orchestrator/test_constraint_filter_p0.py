from __future__ import annotations

from rag_pipeline.orchestrator.constraint_filter import (
    build_zero_results_message,
    check_safety_compliance,
    _filter_course,
    _filter_allergens,
    _filter_calories,
    _filter_exclude_by_title,
    _expand_exclude_term_variants,
)


def test_build_zero_results_message_prioritizes_blocking_constraint():
    msg = build_zero_results_message(
        {
            "exclude_ingredient": ["peanut"],
            "cal_upper_limit": 450,
            "course": "dinner",
        },
        "find_recipe",
    )
    assert "No recipes found under 450 kcal" in msg
    assert "allergen restrictions (peanut) cannot be relaxed" in msg


def test_check_safety_compliance_flags_violating_payload(monkeypatch):
    monkeypatch.setattr(
        "rag_pipeline.orchestrator.constraint_filter._fetch_allergen_violating_ids",
        lambda *_a, **_k: {"r1"},
    )
    result = check_safety_compliance(
        fused=[{"title": "Test", "payload": {"id": "r1", "meal_type": "dinner"}}],
        entities={"exclude_ingredient": ["peanut"], "course": "dinner"},
        intent="find_recipe",
        driver=object(),
    )
    assert result["passed"] is False
    assert any("allergen_violation" in v for v in result["violations"])


# ── Filter A — _filter_course (pure function, zero DB calls) ──────────────────

def _make_item(rid: str, meal_type: str | None, title: str = "Recipe") -> dict:
    payload: dict = {"id": rid, "title": title}
    if meal_type is not None:
        payload["meal_type"] = meal_type
    return {"payload": payload, "sources": ["semantic"]}


def test_filter_course_passes_matching_meal_type():
    fused = [_make_item("r1", "dinner")]
    result = _filter_course(fused, "dinner")
    assert len(result) == 1
    assert result[0]["payload"]["id"] == "r1"


def test_filter_course_drops_non_matching_meal_type():
    fused = [_make_item("r1", "breakfast")]
    result = _filter_course(fused, "dinner")
    assert result == []


def test_filter_course_keeps_missing_meal_type_as_unverified():
    fused = [_make_item("r1", None)]
    result = _filter_course(fused, "dinner")
    assert len(result) == 1
    assert "unverified_course" in result[0]["sources"]


def test_filter_course_skips_invalid_course_value():
    """Non-standard values like 'soup' are dish categories, not meal types — skip filter."""
    fused = [_make_item("r1", "dinner"), _make_item("r2", "breakfast")]
    result = _filter_course(fused, "soup")
    assert len(result) == 2  # both kept — filter skipped


def test_filter_course_empty_fused_returns_empty():
    assert _filter_course([], "lunch") == []


# ── Filter A — mixed batch ─────────────────────────────────────────────────────

def test_filter_course_mixed_batch():
    fused = [
        _make_item("r1", "dinner"),
        _make_item("r2", "breakfast"),
        _make_item("r3", None),
    ]
    result = _filter_course(fused, "dinner")
    ids = [x["payload"]["id"] for x in result]
    assert "r1" in ids
    assert "r2" not in ids
    assert "r3" in ids  # kept as unverified


# ── Filter B — _filter_allergens ──────────────────────────────────────────────

def test_filter_allergens_removes_violating_recipe(monkeypatch):
    monkeypatch.setattr(
        "rag_pipeline.orchestrator.constraint_filter._fetch_allergen_violating_ids",
        lambda *_a, **_k: {"r1"},
    )
    fused = [{"payload": {"id": "r1", "title": "Peanut Soup", "meal_type": "dinner"}, "sources": ["semantic"]}]
    result = _filter_allergens(fused, ["peanut"], driver=object(), database=None)
    assert result == []


def test_filter_allergens_keeps_safe_recipe(monkeypatch):
    monkeypatch.setattr(
        "rag_pipeline.orchestrator.constraint_filter._fetch_allergen_violating_ids",
        lambda *_a, **_k: set(),
    )
    fused = [{"payload": {"id": "r1", "title": "Tofu Stir Fry", "meal_type": "dinner"}, "sources": ["semantic"]}]
    result = _filter_allergens(fused, ["peanut"], driver=object(), database=None)
    assert len(result) == 1


def test_filter_allergens_empty_list_passes_all(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(
        "rag_pipeline.orchestrator.constraint_filter._fetch_allergen_violating_ids",
        lambda *_a, **_k: called.__setitem__("n", called["n"] + 1) or set(),
    )
    fused = [
        {"payload": {"id": "r1", "title": "A", "meal_type": "dinner"}, "sources": []},
        {"payload": {"id": "r2", "title": "B", "meal_type": "lunch"}, "sources": []},
    ]
    result = _filter_allergens(fused, [], driver=object(), database=None)
    assert len(result) == 2
    # _fetch_allergen_violating_ids returns early when allergens=[] before any DB call
    assert called["n"] == 0


def test_filter_allergens_no_id_item_dropped(monkeypatch):
    # Tier 1 change: unverifiable recipes (no DB ID) are now DROPPED when
    # allergens are set, not tagged and kept. A user with a peanut allergy
    # must never receive a recipe whose ingredients we could not verify.
    monkeypatch.setattr(
        "rag_pipeline.orchestrator.constraint_filter._fetch_allergen_violating_ids",
        lambda *_a, **_k: set(),
    )
    fused = [{"payload": {"title": "Mystery Dish"}, "sources": []}]
    result = _filter_allergens(fused, ["peanut"], driver=object(), database=None)
    assert len(result) == 0


# ── Helper — _expand_exclude_term_variants ────────────────────────────────────

def test_expand_variants_singular_from_plural():
    variants = _expand_exclude_term_variants("peanuts")
    assert "peanut" in variants


def test_expand_variants_typo_correction():
    variants = _expand_exclude_term_variants("banannas")
    assert "banana" in variants


def test_expand_variants_ies_to_y():
    variants = _expand_exclude_term_variants("strawberries")
    assert "strawberry" in variants


# ── Helper — _filter_exclude_by_title ─────────────────────────────────────────

def test_filter_exclude_by_title_drops_matching_title():
    fused = [{"payload": {"id": "r1", "title": "Peanut Butter Cookies"}, "sources": []}]
    result = _filter_exclude_by_title(fused, ["peanut"])
    assert result == []


def test_filter_exclude_by_title_keeps_non_matching():
    fused = [{"payload": {"id": "r1", "title": "Tofu Curry"}, "sources": []}]
    result = _filter_exclude_by_title(fused, ["peanut"])
    assert len(result) == 1


def test_filter_exclude_by_title_empty_terms_passes_all():
    fused = [{"payload": {"id": "r1", "title": "Anything"}, "sources": []}]
    result = _filter_exclude_by_title(fused, [])
    assert len(result) == 1


# ── Filter C — _filter_calories ───────────────────────────────────────────────

def test_filter_calories_drops_over_limit_recipe(monkeypatch):
    monkeypatch.setattr(
        "rag_pipeline.orchestrator.constraint_filter._fetch_calorie_violating_ids",
        lambda *_a, **_k: {"r1"},
    )
    fused = [{"payload": {"id": "r1", "title": "Heavy Pasta", "meal_type": "dinner"}, "sources": []}]
    result = _filter_calories(fused, 500, driver=object(), database=None)
    assert result == []


def test_filter_calories_keeps_under_limit_recipe(monkeypatch):
    monkeypatch.setattr(
        "rag_pipeline.orchestrator.constraint_filter._fetch_calorie_violating_ids",
        lambda *_a, **_k: set(),
    )
    fused = [{"payload": {"id": "r1", "title": "Light Salad", "meal_type": "lunch"}, "sources": []}]
    result = _filter_calories(fused, 500, driver=object(), database=None)
    assert len(result) == 1


def test_filter_calories_drops_item_with_no_id(monkeypatch):
    """Strict mode: items with no extractable ID cannot be verified — drop them."""
    monkeypatch.setattr(
        "rag_pipeline.orchestrator.constraint_filter._fetch_calorie_violating_ids",
        lambda *_a, **_k: set(),
    )
    fused = [{"payload": {"title": "Unknown Dish"}, "sources": []}]
    result = _filter_calories(fused, 500, driver=object(), database=None)
    assert result == []
