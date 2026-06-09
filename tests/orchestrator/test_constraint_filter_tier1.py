"""
Tier 1 Safety Gate — PR 1 tests.

Covers the three changes shipped in PR 1:
  1. _fetch_allergen_violating_ids raises AllergenFilterUnavailable on DB error
     (fail closed, not fail open).
  2. _filter_allergens drops unverifiable recipes (no DB ID) when allergens are set,
     instead of tagging and keeping them.
  3. Structured observability log emitted on every _filter_allergens call.

Maya = user with peanut allergy (allergens set).
Sam  = user with no allergens (filter never invoked; unchanged behaviour).
"""

from __future__ import annotations

import pytest

from rag_pipeline.orchestrator.constraint_filter import (
    AllergenFilterUnavailable,
    _fetch_allergen_violating_ids,
    _filter_allergens,
)


# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_recipe(rid: str | None, title: str = "Test Recipe") -> dict:
    """Build a minimal fused-result item. rid=None simulates an unverifiable recipe."""
    if rid:
        return {"title": title, "payload": {"id": rid}}
    return {"title": title, "payload": {}}


def _fake_driver(violating: set[str] | None = None, raises: Exception | None = None):
    """Minimal Neo4j driver stub."""
    class _Session:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def run(self, *_a, **_k):
            if raises:
                raise raises
            return [{"flagged_id": rid} for rid in (violating or set())]

    class _Driver:
        def session(self, **_kw):
            return _Session()

    return _Driver()


# ── 1. fail-closed: _fetch_allergen_violating_ids ─────────────────────────────


def test_db_error_raises_allergen_filter_unavailable():
    driver = _fake_driver(raises=RuntimeError("connection refused"))
    with pytest.raises(AllergenFilterUnavailable):
        _fetch_allergen_violating_ids(driver, ["r1"], ["peanut"], None)


def test_db_error_message_includes_original_cause():
    driver = _fake_driver(raises=ConnectionError("neo4j unreachable"))
    with pytest.raises(AllergenFilterUnavailable, match="neo4j unreachable"):
        _fetch_allergen_violating_ids(driver, ["r1"], ["peanut"], None)


def test_empty_recipe_ids_returns_empty_set_without_db_call():
    # No DB call should happen — guard clause returns early.
    driver = _fake_driver(raises=RuntimeError("should not be called"))
    result = _fetch_allergen_violating_ids(driver, [], ["peanut"], None)
    assert result == set()


def test_empty_allergens_returns_empty_set_without_db_call():
    driver = _fake_driver(raises=RuntimeError("should not be called"))
    result = _fetch_allergen_violating_ids(driver, ["r1"], [], None)
    assert result == set()


def test_successful_query_returns_violating_ids():
    driver = _fake_driver(violating={"r1", "r3"})
    result = _fetch_allergen_violating_ids(driver, ["r1", "r2", "r3"], ["peanut"], None)
    assert result == {"r1", "r3"}


def test_successful_query_empty_violations():
    driver = _fake_driver(violating=set())
    result = _fetch_allergen_violating_ids(driver, ["r1", "r2"], ["peanut"], None)
    assert result == set()


# ── 2. _filter_allergens: drop unverifiable (Maya) ───────────────────────────


def test_unverifiable_recipe_dropped_when_allergens_set(monkeypatch):
    monkeypatch.setattr(
        "rag_pipeline.orchestrator.constraint_filter._fetch_allergen_violating_ids",
        lambda *_a, **_k: set(),
    )
    fused = [_make_recipe(None, "Mystery Dish")]
    result = _filter_allergens(fused, ["peanut"], driver=object(), database=None)
    # Unverifiable — must be dropped, not tagged.
    assert result == []


def test_unverifiable_recipe_not_tagged_and_kept(monkeypatch):
    """Regression: old behaviour was to tag with unverified_allergen and keep."""
    monkeypatch.setattr(
        "rag_pipeline.orchestrator.constraint_filter._fetch_allergen_violating_ids",
        lambda *_a, **_k: set(),
    )
    fused = [_make_recipe(None, "Mystery Dish")]
    result = _filter_allergens(fused, ["peanut"], driver=object(), database=None)
    # Must not be returned with an unverified_allergen tag.
    assert not any("unverified_allergen" in (r.get("sources") or []) for r in result)


def test_confirmed_violating_recipe_dropped(monkeypatch):
    monkeypatch.setattr(
        "rag_pipeline.orchestrator.constraint_filter._fetch_allergen_violating_ids",
        lambda *_a, **_k: {"r1"},
    )
    fused = [_make_recipe("r1", "Peanut Butter Toast")]
    result = _filter_allergens(fused, ["peanut"], driver=object(), database=None)
    assert result == []


def test_safe_verifiable_recipe_kept(monkeypatch):
    monkeypatch.setattr(
        "rag_pipeline.orchestrator.constraint_filter._fetch_allergen_violating_ids",
        lambda *_a, **_k: set(),
    )
    fused = [_make_recipe("r2", "Garden Salad")]
    result = _filter_allergens(fused, ["peanut"], driver=object(), database=None)
    assert len(result) == 1
    assert result[0]["payload"]["id"] == "r2"


def test_mixed_batch_maya(monkeypatch):
    """Safe, violating, and unverifiable in one batch — only safe recipe survives."""
    monkeypatch.setattr(
        "rag_pipeline.orchestrator.constraint_filter._fetch_allergen_violating_ids",
        lambda *_a, **_k: {"r_bad"},
    )
    fused = [
        _make_recipe("r_good", "Grilled Chicken"),      # safe — kept
        _make_recipe("r_bad", "Peanut Stir Fry"),       # allergen — dropped
        _make_recipe(None, "Unknown Recipe"),            # unverifiable — dropped
    ]
    result = _filter_allergens(fused, ["peanut"], driver=object(), database=None)
    assert len(result) == 1
    assert result[0]["payload"]["id"] == "r_good"


def test_db_error_propagates_from_filter_allergens():
    """AllergenFilterUnavailable must propagate — not be caught inside _filter_allergens."""
    driver = _fake_driver(raises=RuntimeError("DB down"))
    fused = [_make_recipe("r1", "Some Recipe")]
    with pytest.raises(AllergenFilterUnavailable):
        _filter_allergens(fused, ["peanut"], driver=driver, database=None)


def test_empty_fused_returns_empty(monkeypatch):
    monkeypatch.setattr(
        "rag_pipeline.orchestrator.constraint_filter._fetch_allergen_violating_ids",
        lambda *_a, **_k: set(),
    )
    result = _filter_allergens([], ["peanut"], driver=object(), database=None)
    assert result == []


# ── 3. Observability log ──────────────────────────────────────────────────────


def test_observability_log_emitted_on_normal_run(monkeypatch, caplog):
    import logging
    monkeypatch.setattr(
        "rag_pipeline.orchestrator.constraint_filter._fetch_allergen_violating_ids",
        lambda *_a, **_k: {"r_bad"},
    )
    fused = [
        _make_recipe("r_good", "Safe Recipe"),
        _make_recipe("r_bad", "Unsafe Recipe"),
        _make_recipe(None, "Unverifiable Recipe"),
    ]
    with caplog.at_level(logging.INFO, logger="rag_pipeline.orchestrator.constraint_filter"):
        _filter_allergens(fused, ["peanut"], driver=object(), database=None)

    log_messages = caplog.messages
    # The summary log should be present.
    assert any("Allergen filter complete" in m for m in log_messages)


def test_observability_log_counts_correct(monkeypatch, caplog):
    import logging
    monkeypatch.setattr(
        "rag_pipeline.orchestrator.constraint_filter._fetch_allergen_violating_ids",
        lambda *_a, **_k: {"r_bad"},
    )
    fused = [
        _make_recipe("r_good", "Safe Recipe"),
        _make_recipe("r_bad", "Unsafe Recipe"),
        _make_recipe(None, "Unverifiable Recipe"),
    ]
    with caplog.at_level(logging.INFO, logger="rag_pipeline.orchestrator.constraint_filter"):
        result = _filter_allergens(fused, ["peanut"], driver=object(), database=None)

    assert len(result) == 1  # only r_good survives

    # Find the summary log record.
    records = [r for r in caplog.records if "Allergen filter complete" in r.getMessage()]
    assert records, "Expected at least one 'Allergen filter complete' log record"
    extra = records[0].__dict__
    assert extra.get("input_count") == 3
    assert extra.get("dropped_violators") == 1
    assert extra.get("dropped_unverifiable") == 1
    assert extra.get("kept_count") == 1
    assert extra.get("allergen_count") == 1
