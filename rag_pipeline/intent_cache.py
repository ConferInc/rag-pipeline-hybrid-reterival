"""
LRU cache for intent extraction. Caches (query -> raw JSON string) to avoid
repeat LLM calls for duplicate/similar queries. Uses stdlib OrderedDict
(no external dependencies).
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


def _normalize_key(text: str, mode: str) -> str:
    if mode == "strip_lower":
        return text.strip().lower()
    if mode == "strip":
        return text.strip()
    return text


def _load_intent_cache_config(config_path: str | Path) -> dict[str, Any]:
    """Load intent_cache section from config YAML."""
    path = Path(config_path)
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            raw = yaml.safe_load(f)
        return (raw or {}).get("intent_cache", {}) or {}
    except Exception:
        return {}


class IntentCache:
    """
    LRU cache for intent extraction output (raw JSON string).
    Keyed by normalized query text.
    """

    def __init__(
        self,
        *,
        max_size: int = 500,
        key_normalize: str = "strip_lower",
    ) -> None:
        self._max_size = max(1, max_size)
        self._key_normalize = key_normalize
        self._cache: OrderedDict[str, str] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, query: str) -> str | None:
        key = _normalize_key(query, self._key_normalize)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                logger.debug(
                    "Intent cache hit",
                    extra={"component": "intent_cache", "key": key[:100], "cache_size": len(self._cache)},
                )
                return self._cache[key]
        return None

    def put(self, query: str, raw_json: str) -> None:
        key = _normalize_key(query, self._key_normalize)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                self._cache[key] = raw_json
            else:
                while len(self._cache) >= self._max_size:
                    self._cache.popitem(last=False)
                self._cache[key] = raw_json
        logger.debug(
            "Intent cache stored",
            extra={"component": "intent_cache", "key": key[:100], "cache_size": len(self._cache)},
        )


# Module-level cache instance, lazily initialized
_intent_cache: IntentCache | None = None
_intent_cache_config_path: str | Path | None = None


def get_intent_cache(config_path: str | Path) -> IntentCache | None:
    """
    Return the IntentCache instance if caching is enabled, else None.
    Creates the cache on first use with config from config_path.
    """
    global _intent_cache, _intent_cache_config_path
    cfg = _load_intent_cache_config(config_path)
    if not cfg.get("enabled", False):
        return None
    path_str = str(config_path)
    if _intent_cache is None or _intent_cache_config_path != path_str:
        _intent_cache = IntentCache(
            max_size=cfg.get("max_size", 500),
            key_normalize=cfg.get("key_normalize", "strip_lower"),
        )
        _intent_cache_config_path = path_str
    return _intent_cache
