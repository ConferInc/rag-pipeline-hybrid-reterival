from __future__ import annotations

from rag_pipeline.augmentation import prompt_builder as pb
from rag_pipeline.orchestrator.orchestrator import OrchestratorResult


def test_prompt_contains_retrieved_results():
    result = OrchestratorResult(
        intent="find_recipe",
        entities={},
        fused_results=[{"key": "r1", "label": "Recipe", "title": "Soup", "sources": ["semantic"], "payload": {"id": "r1", "title": "Soup", "meal_type": "dinner"}}],
    )
    prompt = pb.build_augmented_prompt(result, "find dinner")
    assert "[RANKED CONTEXT]" in prompt
    assert "Soup" in prompt


def test_usda_context_injected_when_flag_enabled(monkeypatch):
    monkeypatch.setenv("ENABLE_USDA_2025_PROMPT_CONTEXT", "1")
    result = OrchestratorResult(intent="find_recipe", entities={"usda_guidelines": {"v": 1}})
    prompt = pb.build_augmented_prompt(result, "query")
    assert "DAILY PATTERN (~2,000 kcal adult)" in prompt
