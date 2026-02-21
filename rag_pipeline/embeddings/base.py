from __future__ import annotations

from typing import Protocol, Sequence


class QueryEmbedder(Protocol):
    def embed_query(self, text: str) -> Sequence[float]: ...

