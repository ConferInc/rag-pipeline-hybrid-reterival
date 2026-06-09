from __future__ import annotations

from rag_pipeline.orchestrator.entity_enrichment import enrich_entities


def test_enrichment_adds_related_entities():
    cfg = {
        "entity_enrichment_enabled": True,
        "entity_fallbacks": {
            "diet_keywords": {"vegan": ["Vegan"]},
            "course_keywords": {"dinner": "dinner"},
        },
    }
    out = enrich_entities("vegan dinner without peanuts", {}, cfg)
    assert out["diet"] == ["Vegan"]
    assert out["course"] == "dinner"


def test_enrichment_deduplicates_results():
    cfg = {
        "entity_enrichment_enabled": True,
        "entity_fallbacks": {"diet_keywords": {"vegan": ["Vegan"]}, "course_keywords": {}},
    }
    out = enrich_entities("vegan", {"diet": ["Vegan"]}, cfg)
    assert out["diet"] == ["Vegan"]
