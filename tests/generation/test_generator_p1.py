from __future__ import annotations

from rag_pipeline.generation import generator


def test_split_prompt_with_and_without_system_section():
    system, user = generator._split_prompt("[SYSTEM]\nrules\n[USER QUERY]\nhello")
    assert "rules" in system
    assert "[USER QUERY]" in user

    system2, user2 = generator._split_prompt("just user text")
    assert system2
    assert "just user text" in user2


def test_generate_response_applies_retry_and_overrides(monkeypatch, tmp_path):
    cfg = tmp_path / "embedding_config.yaml"
    cfg.write_text(
        "generation:\n"
        "  model: test-model\n"
        "  max_tokens: 99\n"
        "  temperature: 0.2\n"
        "llm_retry:\n"
        "  max_attempts: 2\n"
    )

    class FakeClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    FakeClient.kwargs = kwargs
                    msg = type("M", (), {"content": "ok"})()
                    return type("R", (), {"choices": [type("C", (), {"message": msg})()], "model": "m"})()

    monkeypatch.setattr(generator, "OpenAI", lambda **_k: FakeClient)
    monkeypatch.setattr(generator, "with_retry", lambda fn, **_k: fn())
    monkeypatch.delenv("GENERATION_MODEL", raising=False)
    out = generator.generate_response("[SYSTEM]\nX\n[USER QUERY]\nY", config_path=str(cfg))
    assert out == "ok"
    assert FakeClient.kwargs["model"] == "test-model"
    assert FakeClient.kwargs["max_tokens"] == 99
