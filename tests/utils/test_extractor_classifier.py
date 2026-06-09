from __future__ import annotations

from extractor_classifier import parse_extractor_output, sanity_check


def test_parse_extractor_output_repairs_markdown_and_trailing_comma():
    raw = """```json
{"intent":"find_recipe","entities":{"diet":["Vegan"],},}
```"""
    out = parse_extractor_output(raw)
    assert out is not None
    assert out["intent"] == "find_recipe"


def test_sanity_check_rejects_invalid_intent_enum():
    out = sanity_check({"intent": "not_real", "entities": {}})
    assert out is not True
    assert out[0] is False
