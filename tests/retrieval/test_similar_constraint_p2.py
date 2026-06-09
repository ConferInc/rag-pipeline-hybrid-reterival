from __future__ import annotations

from rag_pipeline.retrieval.similar_constraint import retrieve_recipes_from_similar_constraint_users


def test_similar_constraint_empty_profile_returns_empty_context():
    out = retrieve_recipes_from_similar_constraint_users(
        driver=object(),
        diets=[],
        allergens=[],
        health_conditions=[],
    )
    assert out == {"similar_nodes": [], "expanded_context": []}


def test_similar_constraint_handles_driver_error_gracefully():
    class Driver:
        def session(self, database=None):
            class CM:
                def __enter__(self):
                    raise RuntimeError("db down")
                def __exit__(self, *_):
                    return False
            return CM()

    out = retrieve_recipes_from_similar_constraint_users(
        driver=Driver(),
        diets=["Vegan"],
        allergens=[],
        health_conditions=[],
    )
    assert out == {"similar_nodes": [], "expanded_context": []}
