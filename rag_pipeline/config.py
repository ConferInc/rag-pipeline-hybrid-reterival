from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class VectorIndexSpec:
    label: str
    property: str
    dimensions: int
    index_name: str | None = None


@dataclass(frozen=True)
class SemanticConfig:
    write_property: str
    label_text_rules: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class EmbeddingConfig:
    semantic: SemanticConfig
    semantic_vector_indexes: list[VectorIndexSpec]
    structural_vector_indexes: list[VectorIndexSpec]


def load_embedding_config(path: str | Path) -> EmbeddingConfig:
    config_path = Path(path)
    raw = yaml.safe_load(config_path.read_text())

    semantic_raw = raw.get("semantic", {})
    semantic = SemanticConfig(
        write_property=str(semantic_raw["write_property"]),
        label_text_rules=dict(semantic_raw.get("label_text_rules", {})),
    )

    vector_indexes_raw = raw.get("vector_indexes", {})

    semantic_indexes_raw = vector_indexes_raw.get("semantic", [])
    semantic_vector_indexes: list[VectorIndexSpec] = []
    for idx in semantic_indexes_raw:
        semantic_vector_indexes.append(
            VectorIndexSpec(
                label=str(idx["label"]),
                property=str(idx["property"]),
                dimensions=int(idx["dimensions"]),
                index_name=(str(idx["index_name"]) if "index_name" in idx else None),
            )
        )

    structural_indexes_raw = vector_indexes_raw.get("structural", [])
    structural_vector_indexes: list[VectorIndexSpec] = []
    for idx in structural_indexes_raw:
        structural_vector_indexes.append(
            VectorIndexSpec(
                label=str(idx["label"]),
                property=str(idx["property"]),
                dimensions=int(idx["dimensions"]),
                index_name=(str(idx["index_name"]) if "index_name" in idx else None),
            )
        )

    return EmbeddingConfig(
        semantic=semantic,
        semantic_vector_indexes=semantic_vector_indexes,
        structural_vector_indexes=structural_vector_indexes,
    )


def get_semantic_index_spec(
    cfg: EmbeddingConfig, *, label: str, require_index_name: bool = True
) -> VectorIndexSpec:
    matches = [s for s in cfg.semantic_vector_indexes if s.label == label]
    if not matches:
        raise KeyError(f"No semantic vector index configured for label={label!r}")
    if len(matches) > 1:
        raise ValueError(f"Multiple semantic vector indexes configured for label={label!r}")

    spec = matches[0]
    if require_index_name and not spec.index_name:
        raise ValueError(
            f"Missing index_name for semantic vector index label={label!r}. "
            "Add `index_name` under vector_indexes.semantic in embedding_config.yaml."
        )
    return spec


def get_structural_index_spec(
    cfg: EmbeddingConfig, *, label: str, require_index_name: bool = True
) -> VectorIndexSpec:
    matches = [s for s in cfg.structural_vector_indexes if s.label == label]
    if not matches:
        raise KeyError(f"No structural vector index configured for label={label!r}")
    if len(matches) > 1:
        raise ValueError(f"Multiple structural vector indexes configured for label={label!r}")

    spec = matches[0]
    if require_index_name and not spec.index_name:
        raise ValueError(
            f"Missing index_name for structural vector index label={label!r}. "
            "Add `index_name` under vector_indexes.structural in embedding_config.yaml."
        )
    return spec


# ── USDA guideline foundation (Phase A) ────────────────────────────────────

# USDA food groups we track for the 2025 food-pyramid behavior.
USDA_FOOD_GROUPS: tuple[str, ...] = (
    "protein",
    "dairy",
    "vegetables",
    "fruits",
    "whole_grains",
)


@dataclass(frozen=True)
class USDAGroupRule:
    """
    Minimal rule representation for a USDA food group.

    Note: Phase A focuses on contracts and defaults. Scoring logic is added in
    later phases.
    """

    # Target default (units are "group portions" for now; can be adapted later).
    target_default: float
    # Soft threshold used to decide when to reduce bonus or warn.
    soft_threshold: float
    # Lower priority number => higher importance when relaxing in later phases.
    priority: int
    # Weight used in later scoring/ranking.
    weight: float
    # Unit for daily targets (e.g. 'oz_eq', 'cup_eq', 'servings').
    unit: str


@dataclass(frozen=True)
class USDAGuidelineConfig:
    version: str
    # Map group_name -> rule
    groups: dict[str, USDAGroupRule]


def get_default_usda_guidelines() -> USDAGuidelineConfig:
    """
    Deterministic local defaults for USDA 2025 food-group targets.

    Integration with Postgres/Supabase (gold.nutritional_guidelines) is handled
    by `rag_pipeline.orchestrator.usda_guidelines` with fallback to these
    defaults.
    """

    # Priority order is per Phase A relaxation ladder (whole_grains first, protein last).
    # The absolute target values are placeholders until you wire real guideline units.
    order = {
        "whole_grains": 1,
        "fruits": 2,
        "vegetables": 3,
        "dairy": 4,
        "protein": 5,
    }

    groups: dict[str, USDAGroupRule] = {}
    for g in USDA_FOOD_GROUPS:
        unit = {
            "protein": "oz_eq",
            "dairy": "cup_eq",
            "vegetables": "cup_eq",
            "fruits": "cup_eq",
            "whole_grains": "oz_eq",
        }.get(g, "unit")
        groups[g] = USDAGroupRule(
            target_default=1.0,
            soft_threshold=0.8,
            priority=order[g],
            weight=1.0,
            unit=unit,
        )

    return USDAGuidelineConfig(version="usda_2025_default_v1", groups=groups)

