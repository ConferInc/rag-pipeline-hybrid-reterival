from __future__ import annotations

import pytest

from rag_pipeline.config import (
    get_semantic_index_spec,
    load_embedding_config,
)


def test_load_embedding_config_parses_vector_indexes(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(
        "semantic:\n"
        "  write_property: embedding\n"
        "  label_text_rules: {}\n"
        "vector_indexes:\n"
        "  semantic:\n"
        "    - label: Recipe\n"
        "      property: embedding\n"
        "      dimensions: 3\n"
        "      index_name: idx\n"
        "  structural: []\n"
    )
    cfg = load_embedding_config(p)
    assert cfg.semantic_vector_indexes[0].label == "Recipe"


def test_get_semantic_index_spec_duplicate_label_raises(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(
        "semantic:\n"
        "  write_property: embedding\n"
        "  label_text_rules: {}\n"
        "vector_indexes:\n"
        "  semantic:\n"
        "    - {label: Recipe, property: embedding, dimensions: 3, index_name: i1}\n"
        "    - {label: Recipe, property: embedding, dimensions: 3, index_name: i2}\n"
        "  structural: []\n"
    )
    cfg = load_embedding_config(p)
    with pytest.raises(ValueError):
        get_semantic_index_spec(cfg, label="Recipe")
