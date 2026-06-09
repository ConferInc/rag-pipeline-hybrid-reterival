from __future__ import annotations

from chatbot import response_generator as rg
from rag_pipeline.orchestrator.orchestrator import OrchestratorResult


def test_response_generator_template_and_history_formatting():
    txt = rg.get_template_response("greeting", customer_name="Priya")
    assert "Priya" in txt
    hist = rg.format_conversation_history([("user", "hi"), ("assistant", "hello")])
    assert "User: hi" in hist


def test_response_generator_uses_prompt_and_llm(monkeypatch):
    monkeypatch.setattr(rg, "build_augmented_prompt", lambda *_a, **_k: "[SYSTEM]\nS\n[USER QUERY]\nQ")
    monkeypatch.setattr(rg, "generate_response", lambda prompt, **_k: f"ok::{prompt[:10]}")
    out = rg.generate_chat_response(
        OrchestratorResult(intent="find_recipe", entities={}, fused_results=[]),
        "query",
        "User: hi",
    )
    assert out.startswith("ok::")
