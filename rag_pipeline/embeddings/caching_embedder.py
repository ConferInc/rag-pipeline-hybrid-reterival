"""
LRU cache wrapper for QueryEmbedder. Caches embed_query(text) by normalized key
to avoid repeat API calls for duplicate/similar queries. Uses stdlib OrderedDict
(no external dependencies).
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Sequence

from rag_pipeline.embeddings.base import QueryEmbedder

logger = logging.getLogger(__name__)


def _normalize_key(text: str, mode: str) -> str:
    if mode == "strip_lower":
        return text.strip().lower()
    if mode == "strip":
        return text.strip()
    return text


class CachingQueryEmbedder:
    """
    Wraps a QueryEmbedder and caches embed_query() results by normalized query text.
    Uses an LRU eviction policy when max_size is exceeded.
    """

    def __init__(
        self,
        delegate: QueryEmbedder,
        *,
        max_size: int = 500,
        key_normalize: str = "strip_lower",
    ) -> None:
        self._delegate = delegate
        self._max_size = max(1, max_size)
        self._key_normalize = key_normalize
        # OrderedDict: oldest (LRU) at front, newest at back
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._lock = threading.Lock()

    def embed_query(self, text: str) -> Sequence[float]:
        key = _normalize_key(text, self._key_normalize)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                logger.debug(
                    "Embedding cache hit",
                    extra={"component": "embedding_cache", "key": key[:100], "cache_size": len(self._cache)},
                )
                return self._cache[key].copy()

        logger.debug(
            "Embedding cache miss",
            extra={"component": "embedding_cache", "key": key[:100]},
        )
        vector = list(self._delegate.embed_query(text))

        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                self._cache[key] = vector
            else:
                while len(self._cache) >= self._max_size:
                    self._cache.popitem(last=False)
                self._cache[key] = vector

        return vector
