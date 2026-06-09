from __future__ import annotations

import asyncio

import pytest

from rag_pipeline.config import EmbeddingConfig, SemanticConfig
from rag_pipeline.orchestrator import orchestrator


def _cfg() -> EmbeddingConfig:
    return EmbeddingConfig(
        semantic=SemanticConfig(write_property="embedding", label_text_rules={}),
        semantic_vector_indexes=[],
        structural_vector_indexes=[],
    )


@pytest.mark.asyncio
async def test_orchestrate_uses_intent_override_and_sets_fallback(monkeypatch, tmp_path):
    cfg_path = tmp_path / "embedding_config.yaml"
    cfg_path.write_text(
        "retrieval_guardrails:\n"
        "  timeout_ms: 1000\n"
        "  keyword:\n"
        "    enabled: false\n"
        "intent_extraction:\n"
        "  confidence_threshold: 0.7\n"
    )

    def fake_to_thread(func, *args, **kwargs):
        return asyncio.create_task(asyncio.sleep(0, result=func(*args, **kwargs)))

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(orchestrator, "retrieve_semantic", lambda *a, **k: [])
    monkeypatch.setattr(orchestrator, "run_cypher_retrieval", lambda *a, **k: [])
    monkeypatch.setattr(orchestrator, "apply_rrf", lambda *a, **k: [])
    monkeypatch.setattr(orchestrator, "apply_hard_constraints", lambda *a, **k: [])
    monkeypatch.setattr(orchestrator, "build_zero_results_message", lambda e, i: "No safe matches")

    out = await orchestrator.orchestrate(
        driver=object(),
        cfg=_cfg(),
        embedder=object(),
        user_query="find me dinner",
        config_path=str(cfg_path),
        intent_override="find_recipe",
        entities_override={"course": "dinner"},
    )
    assert out.intent == "find_recipe"
    assert out.fallback_message == "No safe matches"


@pytest.mark.asyncio
async def test_orchestrate_aggregated_profile_uses_similar_constraint(monkeypatch, tmp_path):
    cfg_path = tmp_path / "embedding_config.yaml"
    cfg_path.write_text(
        "retrieval_guardrails:\n"
        "  timeout_ms: 1000\n"
        "  keyword:\n"
        "    enabled: false\n"
        "intent_extraction:\n"
        "  confidence_threshold: 0.7\n"
    )

    called = {"similar": 0, "struct": 0}

    def fake_to_thread(func, *args, **kwargs):
        return asyncio.create_task(asyncio.sleep(0, result=func(*args, **kwargs)))

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(orchestrator, "retrieve_semantic", lambda *a, **k: [])
    monkeypatch.setattr(orchestrator, "run_cypher_retrieval", lambda *a, **k: [])
    monkeypatch.setattr(orchestrator, "apply_rrf", lambda *a, **k: [])
    monkeypatch.setattr(orchestrator, "apply_hard_constraints", lambda rows, *_a, **_k: rows)
    monkeypatch.setattr(orchestrator, "build_zero_results_message", lambda e, i: "none")
    monkeypatch.setattr(
        orchestrator,
        "retrieve_recipes_from_similar_constraint_users",
        lambda *a, **k: called.__setitem__("similar", called["similar"] + 1) or {"expanded_context": []},
    )
    monkeypatch.setattr(
        orchestrator,
        "structural_search_with_expansion",
        lambda *a, **k: called.__setitem__("struct", called["struct"] + 1) or {},
    )

    await orchestrator.orchestrate(
        driver=object(),
        cfg=_cfg(),
        embedder=object(),
        user_query="family dinner",
        config_path=str(cfg_path),
        intent_override="find_recipe",
        entities_override={},
        customer_profile={"diets": ["Vegan"], "allergens": ["peanut"]},
        is_aggregated_profile=True,
    )
    assert called["similar"] == 1
    assert called["struct"] == 0


@pytest.mark.asyncio
async def test_orchestrate_timeout_in_semantic_path_fails_open(monkeypatch, tmp_path):
    cfg_path = tmp_path / "embedding_config.yaml"
    cfg_path.write_text(
        "retrieval_guardrails:\n"
        "  timeout_ms: 1000\n"
        "  keyword:\n"
        "    enabled: false\n"
        "intent_extraction:\n"
        "  confidence_threshold: 0.7\n"
    )

    def fake_to_thread(func, *args, **kwargs):
        return asyncio.create_task(asyncio.sleep(0, result=func(*args, **kwargs)))

    call_count = {"n": 0}
    real_wait_for = asyncio.wait_for

    async def fake_wait_for(awaitable, timeout):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise asyncio.TimeoutError()
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
    monkeypatch.setattr(orchestrator, "retrieve_semantic", lambda *a, **k: [{"dummy": 1}])
    monkeypatch.setattr(orchestrator, "run_cypher_retrieval", lambda *a, **k: [{"payload": {"id": "r1", "title": "T", "meal_type": "dinner"}}])
    monkeypatch.setattr(orchestrator, "apply_rrf", lambda *_a, **_k: [{"payload": {"id": "r1", "title": "T", "meal_type": "dinner"}}])
    monkeypatch.setattr(orchestrator, "apply_hard_constraints", lambda rows, *_a, **_k: rows)
    monkeypatch.setattr(orchestrator, "build_zero_results_message", lambda *_a, **_k: "none")

    out = await orchestrator.orchestrate(
        driver=object(),
        cfg=_cfg(),
        embedder=object(),
        user_query="find me dinner",
        config_path=str(cfg_path),
        intent_override="find_recipe",
        entities_override={"course": "dinner"},
    )
    assert out is not None
    assert isinstance(out.fused_results, list)
