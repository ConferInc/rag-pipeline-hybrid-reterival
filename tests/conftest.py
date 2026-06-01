"""Shared pytest fixtures for unit tests.

All fixtures here are intentionally lightweight and dependency-free so tests can
stay hermetic and avoid live calls to external systems.
"""

from __future__ import annotations

import sys
import time
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


# Ensure repository root is importable for tests.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def fixed_time(monkeypatch: pytest.MonkeyPatch) -> int:
    """Freeze time.time() for deterministic TTL/rate-limit tests."""
    frozen_epoch = 1_700_000_000
    monkeypatch.setattr(time, "time", lambda: frozen_epoch)
    return frozen_epoch


@pytest.fixture
def env_flags(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Set common test-safe env defaults for feature-flagged behavior."""
    defaults = {
        "ENABLE_USDA_2025_PROMPT_CONTEXT": "false",
        "USDA_STRICT_MODE": "false",
        "OPENAI_API_KEY": "test-key",
        "NEO4J_URI": "bolt://localhost:7687",
        "NEO4J_USERNAME": "neo4j",
        "NEO4J_PASSWORD": "test-password",
        "REDIS_URL": "redis://localhost:6379/0",
        "SUPABASE_URL": "https://test.supabase.co",
        "SUPABASE_KEY": "test-supabase-key",
    }
    for key, value in defaults.items():
        monkeypatch.setenv(key, value)
    return SimpleNamespace(**defaults)


@pytest.fixture
def neo4j_result_rows() -> list[dict]:
    """Sample graph rows used by retrieval/orchestrator tests."""
    return [
        {"id": "recipe-1", "name": "Veg Bowl", "score": 0.91},
        {"id": "recipe-2", "name": "Tofu Curry", "score": 0.83},
    ]


@pytest.fixture
def mock_neo4j_session(neo4j_result_rows: list[dict]) -> MagicMock:
    """Mock Neo4j session returning deterministic row-like records."""
    session = MagicMock(name="neo4j_session")
    result = MagicMock(name="neo4j_result")
    result.data.return_value = neo4j_result_rows
    result.__iter__.return_value = iter(neo4j_result_rows)
    session.run.return_value = result
    return session


@pytest.fixture
def mock_neo4j_driver(mock_neo4j_session: MagicMock) -> MagicMock:
    """Mock Neo4j driver with context-manager aware session()."""
    driver = MagicMock(name="neo4j_driver")
    session_cm = MagicMock(name="neo4j_session_cm")
    session_cm.__enter__.return_value = mock_neo4j_session
    session_cm.__exit__.return_value = False
    driver.session.return_value = session_cm
    return driver


@pytest.fixture
def mock_openai_client() -> MagicMock:
    """Mock OpenAI client for both completion and embeddings paths."""
    client = MagicMock(name="openai_client")

    # Chat response shape
    choice = MagicMock()
    choice.message.content = "mocked llm response"
    chat_response = MagicMock()
    chat_response.choices = [choice]
    client.chat.completions.create.return_value = chat_response

    # Embedding response shape
    emb = MagicMock()
    emb.embedding = [0.1, 0.2, 0.3]
    embedding_response = MagicMock()
    embedding_response.data = [emb]
    client.embeddings.create.return_value = embedding_response
    return client


@pytest.fixture
def mock_supabase_client() -> MagicMock:
    """Mock Supabase client with chainable table/select/execute."""
    client = MagicMock(name="supabase_client")
    table = client.table.return_value
    table.select.return_value = table
    table.eq.return_value = table
    execute_result = MagicMock()
    execute_result.data = []
    table.execute.return_value = execute_result
    return client


@pytest.fixture
def mock_redis_client() -> MagicMock:
    """Mock Redis-like client for cache tests."""
    client = MagicMock(name="redis_client")
    client.get.return_value = None
    client.set.return_value = True
    return client


@pytest.fixture
def sample_profile() -> dict:
    """Representative profile payload for orchestrator/sanitizer tests."""
    return {
        "allergens": ["peanut", "shellfish"],
        "diets": ["Vegan"],
        "context": {"targetCalories": 2000, "mealsPerDay": 3},
    }


@pytest.fixture
def sample_entities() -> dict:
    """Representative extracted entities payload."""
    return {
        "meal_type": "dinner",
        "exclude_recipe_ids": ["recipe-0"],
        "cal_upper_limit": 700,
        "allergens": ["peanut"],
    }


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block outbound network calls to keep unit tests hermetic."""

    real_connect = socket.socket.connect

    def guarded_connect(self, address):
        host = address[0] if isinstance(address, tuple) and address else ""
        allowed = {"localhost", "127.0.0.1", "::1"}
        if host not in allowed:
            raise RuntimeError(f"External network disabled during unit tests: {host}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
