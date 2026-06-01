from __future__ import annotations

from rag_pipeline.orchestrator.cypher_runner import _canonicalize_cypher_row, run_cypher_retrieval


def test_cypher_runner_maps_rows_to_expected_shape():
    row = {"id": "r1", "title": "Soup", "meal_type": "dinner", "collab_score": 0.3}
    out = _canonicalize_cypher_row(row, intent="find_recipe", rank=2)
    assert out["source"] == "cypher"
    assert out["payload"]["id"] == "r1"
    assert out["score_raw"] == 0.5


def test_cypher_runner_handles_query_failure_with_safe_fallback(monkeypatch):
    monkeypatch.setattr(
        "rag_pipeline.orchestrator.cypher_runner.generate_cypher_query",
        lambda *_a, **_k: ("MATCH (n) RETURN n", {}),
    )

    class Driver:
        def session(self, database=None):
            class CM:
                def __enter__(self):
                    raise RuntimeError("db down")
                def __exit__(self, *_):
                    return False
            return CM()

    out = run_cypher_retrieval(Driver(), intent="find_recipe", entities={})
    assert out == []
