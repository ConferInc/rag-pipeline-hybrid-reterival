from __future__ import annotations

from chatbot import context_expander as ce


def test_expands_vague_query_with_history(monkeypatch):
    monkeypatch.setattr(
        ce,
        "OpenAI",
        lambda **_k: type(
            "C",
            (),
            {
                "chat": type(
                    "Chat",
                    (),
                    {
                        "completions": type(
                            "Comp",
                            (),
                            {
                                "create": staticmethod(
                                    lambda **_kw: type(
                                        "R",
                                        (),
                                        {"choices": [type("X", (), {"message": type("M", (), {"content": "expanded query"})()})()]},
                                    )()
                                )
                            },
                        )
                    },
                )
            },
        ),
    )
    out = ce.expand_query_with_context("what about that?", [("user", "find vegan meals"), ("assistant", "Here are options")])
    assert out == "expanded query"


def test_more_options_fallback_uses_previous_context(monkeypatch):
    monkeypatch.setattr(ce, "OpenAI", lambda **_k: (_ for _ in ()).throw(RuntimeError("fail")))
    out = ce.expand_query_with_context(
        "more options",
        [("user", "alternatives to paneer"), ("assistant", "You can use tofu instead of paneer")],
        max_retries=0,
    )
    assert "substitutes for paneer" in out.lower()
