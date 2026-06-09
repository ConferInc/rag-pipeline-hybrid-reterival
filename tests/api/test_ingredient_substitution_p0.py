from __future__ import annotations

from api import ingredient_substitution as sub


def test_substitution_flow_graph_then_semantic_then_llm_fallback_order(monkeypatch):
    calls = {"semantic": 0, "llm": 0}
    monkeypatch.setattr(sub, "get_ingredient_name_by_id", lambda *_a, **_k: "butter")
    monkeypatch.setattr(
        sub,
        "fetch_graph_substitutes",
        lambda *_a, **_k: [{"ingredient_id": "g1", "name": "ghee", "reason": "graph", "source": "graph", "score": 1.0}],
    )
    monkeypatch.setattr(
        sub,
        "fetch_semantic_substitutes",
        lambda *_a, **_k: calls.__setitem__("semantic", calls["semantic"] + 1) or [],
    )
    monkeypatch.setattr(
        sub,
        "llm_substitution_fallback",
        lambda *_a, **_k: calls.__setitem__("llm", calls["llm"] + 1) or [],
    )
    monkeypatch.setattr(sub, "fetch_nutrition_for_ingredients", lambda *_a, **_k: {})

    out = sub.run_ingredient_substitution(
        driver=object(),
        cfg=object(),
        embedder=object(),
        ingredient_id="orig",
        limit=3,
    )
    assert len(out["substitutions"]) == 1
    assert calls["semantic"] == 0
    assert calls["llm"] == 0


def test_substitution_filters_allergen_and_diet_violations(monkeypatch):
    monkeypatch.setattr(sub, "get_ingredient_name_by_id", lambda *_a, **_k: "milk")
    monkeypatch.setattr(sub, "fetch_graph_substitutes", lambda *_a, **_k: [])
    monkeypatch.setattr(
        sub,
        "fetch_semantic_substitutes",
        lambda *_a, **_k: [
            {"ingredient_id": "a", "name": "soy milk", "reason": "x", "source": "semantic", "score": 0.9},
            {"ingredient_id": "b", "name": "almond milk", "reason": "x", "source": "semantic", "score": 0.8},
            {"ingredient_id": "c", "name": "oat milk", "reason": "x", "source": "semantic", "score": 0.7},
        ],
    )
    monkeypatch.setattr(sub, "filter_allergen_violating_ingredients", lambda *_a, **_k: {"b"})
    monkeypatch.setattr(sub, "filter_diet_violating_ingredients", lambda *_a, **_k: {"a"})
    monkeypatch.setattr(sub, "llm_substitution_fallback", lambda *_a, **_k: [])
    monkeypatch.setattr(sub, "fetch_nutrition_for_ingredients", lambda *_a, **_k: {})

    out = sub.run_ingredient_substitution(
        driver=object(),
        cfg=object(),
        embedder=object(),
        ingredient_id="orig",
        customer_allergens=["tree_nuts"],
        customer_diets=["vegan"],
        limit=5,
    )
    assert [x["ingredient_id"] for x in out["substitutions"]] == ["c"]


def test_substitution_uses_llm_fallback_when_no_candidates(monkeypatch):
    monkeypatch.setattr(sub, "get_ingredient_name_by_id", lambda *_a, **_k: "butter")
    monkeypatch.setattr(sub, "fetch_graph_substitutes", lambda *_a, **_k: [])
    monkeypatch.setattr(sub, "fetch_semantic_substitutes", lambda *_a, **_k: [])
    monkeypatch.setattr(
        sub,
        "llm_substitution_fallback",
        lambda *_a, **_k: [{"ingredient_id": "", "name": "olive oil", "reason": "llm", "source": "llm", "score": 0.9}],
    )
    monkeypatch.setattr(sub, "fetch_nutrition_for_ingredients", lambda *_a, **_k: {})

    out = sub.run_ingredient_substitution(
        driver=object(),
        cfg=object(),
        embedder=object(),
        ingredient_id="orig",
        limit=3,
    )
    assert out["substitutions"][0]["source"] == "llm"
