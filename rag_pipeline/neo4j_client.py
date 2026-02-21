from __future__ import annotations

import os
from dataclasses import dataclass

from neo4j import GraphDatabase
from neo4j import Driver


@dataclass(frozen=True)
class Neo4jSettings:
    uri: str
    username: str
    password: str
    database: str | None = None


def neo4j_settings_from_env(
    *,
    uri_env: str = "NEO4J_URI",
    username_env: str = "NEO4J_USERNAME",
    password_env: str = "NEO4J_PASSWORD",
    database_env: str = "NEO4J_DATABASE",
) -> Neo4jSettings:
    uri = os.environ.get(uri_env)
    username = os.environ.get(username_env)
    password = os.environ.get(password_env)
    database = os.environ.get(database_env) or None

    missing = [k for k, v in [(uri_env, uri), (username_env, username), (password_env, password)] if not v]
    if missing:
        raise EnvironmentError(f"Missing required environment variables: {', '.join(missing)}")

    return Neo4jSettings(uri=str(uri), username=str(username), password=str(password), database=database)


def create_neo4j_driver(settings: Neo4jSettings) -> Driver:
    return GraphDatabase.driver(settings.uri, auth=(settings.username, settings.password))

