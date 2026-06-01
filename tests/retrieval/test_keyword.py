from __future__ import annotations

from rag_pipeline.retrieval.keyword import _build_lucene_query, keyword_search


def test_build_lucene_query_escapes_specials_and_adds_wildcards():
    q = _build_lucene_query("chicken (curry)+")
    assert "chicken*" in q
    assert r"\(curry\)\+*" in q


def test_keyword_search_empty_query_returns_empty(mock_neo4j_driver):
    assert keyword_search(mock_neo4j_driver, query="   ") == []


def test_keyword_search_fail_open_on_driver_error(mock_neo4j_driver):
    mock_neo4j_driver.session.side_effect = RuntimeError("db unavailable")
    assert keyword_search(mock_neo4j_driver, query="vegan dinner") == []
