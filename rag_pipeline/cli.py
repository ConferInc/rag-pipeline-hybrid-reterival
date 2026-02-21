from __future__ import annotations

import json
import os
from argparse import ArgumentParser
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from rag_pipeline.config import load_embedding_config
from rag_pipeline.embeddings.openai_embedder import OpenAIQueryEmbedder
from rag_pipeline.neo4j_client import create_neo4j_driver, neo4j_settings_from_env
from rag_pipeline.retrieval.service import SemanticRetrievalRequest, retrieve_semantic


def build_parser() -> ArgumentParser:
    p = ArgumentParser(description="RAG Pipeline CLI")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("semantic-search", help="Run semantic vector search in Neo4j")
    s.add_argument("--config", default="embedding_config.yaml", help="Path to embedding config YAML")
    s.add_argument("--query", required=True, help="User query text")
    s.add_argument("--label", default=None, help="Optional label override (e.g., Recipe)")
    s.add_argument("--top-k", type=int, default=10, help="Top K results")
    return p


def main() -> None:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "semantic-search":
        cfg = load_embedding_config(Path(args.config))

        model = os.environ.get("OPENAI_EMBEDDING_MODEL")
        if not model:
            raise EnvironmentError("Missing required environment variable: OPENAI_EMBEDDING_MODEL")

        embedder = OpenAIQueryEmbedder(client=OpenAI(), model=model)

        neo_settings = neo4j_settings_from_env()
        driver = create_neo4j_driver(neo_settings)

        try:
            results = retrieve_semantic(
                driver,
                cfg=cfg,
                embedder=embedder,
                request=SemanticRetrievalRequest(
                    query=str(args.query), top_k=int(args.top_k), label=args.label
                ),
                database=neo_settings.database,
            )
        finally:
            driver.close()

        print(json.dumps([r.to_dict() for r in results], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

