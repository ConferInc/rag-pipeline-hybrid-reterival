from __future__ import annotations

from rag_pipeline.config import EmbeddingConfig, SemanticConfig, VectorIndexSpec
from rag_pipeline.retrieval.structural import filter_by_intent, get_seed_embedding


def _cfg() -> EmbeddingConfig:
    return EmbeddingConfig(
        semantic=SemanticConfig(write_property="embedding", label_text_rules={}),
        semantic_vector_indexes=[],
        structural_vector_indexes=[
            VectorIndexSpec(
                label="B2C_Customer",
                property="graphSageEmbedding",
                dimensions=3,
                index_name="struct_idx",
            )
        ],
    )


def test_get_seed_embedding_elementid_fallback(mock_neo4j_driver):
    session_cm = mock_neo4j_driver.session.return_value
    session = session_cm.__enter__.return_value
    first = type("R", (), {"single": lambda self: None})()
    second = type("R", (), {"single": lambda self: {"embedding": [0.1, 0.2, 0.3]}})()
    session.run.side_effect = [first, second]

    out = get_seed_embedding(
        mock_neo4j_driver,
        cfg=_cfg(),
        label="B2C_Customer",
        node_id="node-123",
    )
    assert out == [0.1, 0.2, 0.3]


def test_filter_by_intent_labels_and_relationships():
    expanded = [
        {"connected_labels": ["Recipe"], "relationship": "HAS_INGREDIENT"},
        {"connected_labels": ["Ingredient"], "relationship": "HAS_INGREDIENT"},
        {"connected_labels": ["Recipe"], "relationship": "SIMILAR_TO"},
    ]
    out = filter_by_intent(
        expanded,
        allowed_labels=["Recipe"],
        allowed_relationships=["SIMILAR_TO"],
    )
    assert out == [{"connected_labels": ["Recipe"], "relationship": "SIMILAR_TO"}]
