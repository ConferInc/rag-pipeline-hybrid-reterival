from __future__ import annotations

from rag_pipeline.embeddings.openai_embedder import OpenAIQueryEmbedder


def test_openai_embedder_calls_client_with_expected_model():
    class Client:
        class embeddings:
            @staticmethod
            def create(**kwargs):
                Client.kw = kwargs
                return type("R", (), {"data": [type("D", (), {"embedding": [0.1, 0.2]})()]})()

    emb = OpenAIQueryEmbedder(client=Client, model="m1")
    out = emb.embed_query("abc")
    assert out == [0.1, 0.2]
    assert Client.kw["model"] == "m1"
