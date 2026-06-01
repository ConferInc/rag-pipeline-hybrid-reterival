from __future__ import annotations

import pytest

from rag_pipeline.neo4j_client import Neo4jSettings, neo4j_settings_from_env


def test_neo4j_settings_from_env_missing_vars_raises(monkeypatch):
    for key in ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(EnvironmentError):
        neo4j_settings_from_env()


def test_neo4j_settings_from_env_success(monkeypatch):
    monkeypatch.setenv("NEO4J_URI", "bolt://x")
    monkeypatch.setenv("NEO4J_USERNAME", "u")
    monkeypatch.setenv("NEO4J_PASSWORD", "p")
    s = neo4j_settings_from_env()
    assert isinstance(s, Neo4jSettings)
    assert s.uri == "bolt://x"
