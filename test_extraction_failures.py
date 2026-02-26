#!/usr/bin/env python3
"""Smoke tests for intent/entity extraction failure handling."""

from extractor_classifier import parse_extractor_output, sanity_check

# Test parse_extractor_output - valid JSON
valid = '{"intent":"find_recipe","entities":{"course":"lunch"}}'
p = parse_extractor_output(valid)
assert p is not None and p["intent"] == "find_recipe", f"valid: {p}"

# Test repair: trailing comma
broken = '{"intent":"find_recipe","entities":{"course":"lunch",}}'
p2 = parse_extractor_output(broken)
assert p2 is not None and p2["intent"] == "find_recipe", f"broken: {p2}"

# Test markdown strip
md = '```json\n{"intent":"find_recipe","entities":{}}\n```'
p3 = parse_extractor_output(md)
assert p3 is not None and p3["intent"] == "find_recipe", f"md: {p3}"

print("parse_extractor_output OK")

from rag_pipeline.orchestrator.entity_enrichment import enrich_entities

# Disabled - no change
e = enrich_entities("vegan lunch", {}, {"entity_enrichment_enabled": False})
assert e == {}

# Enabled - adds diet and course
e2 = enrich_entities(
    "vegan lunch",
    {},
    {
        "entity_enrichment_enabled": True,
        "entity_fallbacks": {
            "diet_keywords": {"vegan": ["Vegan"]},
            "course_keywords": {"lunch": "lunch"},
        },
    },
)
assert "diet" in e2 and "Vegan" in e2.get("diet", [])
assert e2.get("course") == "lunch"

print("enrich_entities OK")
print("All smoke tests passed")
