from __future__ import annotations

import pytest

from rag_pipeline.config import EmbeddingConfig, SemanticConfig, VectorIndexSpec
from rag_pipeline.retrieval.semantic import semantic_search_by_label


def _cfg() -> EmbeddingConfig:
    return EmbeddingConfig(
        semantic=SemanticConfig(write_property="embedding", label_text_rules={}),
        semantic_vector_indexes=[
            VectorIndexSpec(
                label="Recipe",
                property="embedding",
                dimensions=3,
                index_name="recipe_semantic_idx",
            )
        ],
        structural_vector_indexes=[],
    )


def test_semantic_search_dimension_mismatch_raises(mock_neo4j_driver):
    with pytest.raises(ValueError, match="dimension mismatch"):
        semantic_search_by_label(
            mock_neo4j_driver,
            cfg=_cfg(),
            label="Recipe",
            query_vector=[0.1, 0.2],
        )


def test_semantic_search_drops_recipe_without_required_payload(mock_neo4j_driver):
    session_cm = mock_neo4j_driver.session.return_value
    session = session_cm.__enter__.return_value
    session.run.return_value = iter(
        [
            {"node_id": "1", "labels": ["Recipe"], "node": {"id": "1"}, "score": 0.9},
            {
                "node_id": "2",
                "labels": ["Recipe"],
                "node": {"id": "2", "title": "Soup", "meal_type": "dinner"},
                "score": 0.8,
            },
        ]
    )

    out = semantic_search_by_label(
        mock_neo4j_driver,
        cfg=_cfg(),
        label="Recipe",
        query_vector=[0.1, 0.2, 0.3],
    )
    assert len(out) == 1
    assert out[0].node_id == "2"
