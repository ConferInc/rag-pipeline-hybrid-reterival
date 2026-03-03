"""
LRU cache for label inference (semantic search label from query).
Caches (query -> label) to avoid repeat LLM calls. Uses stdlib OrderedDict.
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


def _load_label_cache_config(config_path: str | Path) -> dict[str, Any]:
    """Load label_cache section from config YAML."""
    path = Path(config_path)
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            raw = yaml.safe_load(f)
        return (raw or {}).get("label_cache", {}) or {}
    except Exception:
        return {}


class LabelCache:
    """LRU cache for label inference output."""

    def __init__(self, *, max_size: int = 500, key_normalize: str = "strip_lower") -> None:
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
                    "Label cache hit",
                    extra={"component": "label_cache", "key": key[:100], "cache_size": len(self._cache)},
                )
                return self._cache[key]
        return None

    def put(self, query: str, label: str) -> None:
        key = _normalize_key(query, self._key_normalize)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                self._cache[key] = label
            else:
                while len(self._cache) >= self._max_size:
                    self._cache.popitem(last=False)
                self._cache[key] = label
        logger.debug(
            "Label cache stored",
            extra={"component": "label_cache", "key": key[:100], "label": label},
        )


_label_cache: LabelCache | None = None
_label_cache_config_path: str | Path | None = None


def get_label_cache(config_path: str | Path) -> LabelCache | None:
    """Return LabelCache if enabled, else None."""
    global _label_cache, _label_cache_config_path
    cfg = _load_label_cache_config(config_path)
    if not cfg.get("enabled", False):
        return None
    path_str = str(config_path)
    if _label_cache is None or _label_cache_config_path != path_str:
        _label_cache = LabelCache(
            max_size=cfg.get("max_size", 500),
            key_normalize=cfg.get("key_normalize", "strip_lower"),
        )
        _label_cache_config_path = path_str
    return _label_cache
