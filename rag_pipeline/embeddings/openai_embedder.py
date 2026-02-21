from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from openai import OpenAI


@dataclass
class OpenAIQueryEmbedder:
    client: OpenAI
    model: str

    def embed_query(self, text: str) -> Sequence[float]:
        resp = self.client.embeddings.create(model=self.model, input=text)
        return resp.data[0].embedding

