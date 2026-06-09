from __future__ import annotations

from rag_pipeline import cli


def test_cli_parse_args_defaults():
    parser = cli.build_parser()
    args = parser.parse_args(["semantic-search", "--query", "hello"])
    assert args.command == "semantic-search"
    assert args.top_k == 10


def test_cli_cache_wrapper_enabled(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("embedding_cache:\n  enabled: true\n  max_size: 2\n")

    class D:
        def embed_query(self, text):
            return [1.0]

    wrapped = cli._maybe_wrap_embedder_with_cache(D(), cfg)
    assert hasattr(wrapped, "embed_query")
