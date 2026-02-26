"""
Logging utilities for RAG pipeline.

- Structured JSON logging with configurable format
- Sensitive data sanitization (truncate query, hash IDs)
- Each module uses its own logger via logging.getLogger(__name__)
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

COMPONENT = "component"
REQUEST_ID = "request_id"

# Context var for request_id; set at CLI/API entry, available to all loggers
_request_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)


def set_request_id(request_id: str | None) -> None:
    """Set request_id for the current context."""
    _request_id_ctx.set(request_id)


def get_request_id() -> str | None:
    """Get request_id from current context."""
    return _request_id_ctx.get()


class RequestIdFilter(logging.Filter):
    """Inject request_id from context into log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        rid = _request_id_ctx.get()
        if rid is not None:
            record.request_id = rid
        return True

# Default truncation for PII-sensitive fields
DEFAULT_TRUNCATE_QUERY_MAX = 200


def truncate_for_log(text: str | None, max_len: int = DEFAULT_TRUNCATE_QUERY_MAX) -> str:
    """
    Truncate text for safe logging (avoids full PII in logs).

    Args:
        text: Raw text (e.g. user query)
        max_len: Max characters to keep

    Returns:
        Truncated string with "..." suffix if truncated
    """
    if text is None:
        return ""
    s = str(text).strip()
    if len(s) <= max_len:
        return s
    return s[:max_len] + "..."


def hash_for_log(value: str | None) -> str:
    """
    Hash a value for logging (e.g. customer node ID). Never log raw IDs.

    Args:
        value: Raw value (e.g. Neo4j elementId)

    Returns:
        First 16 chars of SHA256 hex digest, or "none" if empty
    """
    if not value:
        return "none"
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()
    return digest[:16]


class JsonFormatter(logging.Formatter):
    """
    Format log records as JSON. Includes extra fields (request_id, component, etc.).
    """

    _SKIP_ATTRS = {
        "name", "msg", "args", "created", "filename", "funcName", "levelname",
        "levelno", "lineno", "module", "msecs", "pathname", "process",
        "processName", "relativeCreated", "stack_info", "exc_info", "exc_text",
        "thread", "threadName", "message", "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        obj: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "level": record.levelname,
            "message": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k not in self._SKIP_ATTRS and v is not None:
                obj[k] = v
        if record.exc_info:
            obj["error"] = self.formatException(record.exc_info)
        return json.dumps(obj, default=str)


def setup_pipeline_logging(
    config_path: str | Path = "embedding_config.yaml",
    *,
    level: str | None = None,
    format_type: str | None = None,
    force: bool = False,
) -> None:
    """
    Configure root logger for pipeline. Loads settings from config if present.

    Args:
        config_path: Path to embedding_config.yaml (for logging section)
        level: Override log level (DEBUG, INFO, WARN, ERROR, CRITICAL)
        format_type: Override format ("json" or "human")
        force: Reconfigure even if already configured
    """
    cfg: dict[str, Any] = {}
    path = Path(config_path)
    if path.exists():
        try:
            with open(path) as f:
                raw = yaml.safe_load(f)
            cfg = raw.get("logging", {}) or {}
        except Exception:
            pass

    log_level = level or cfg.get("level", "INFO")
    fmt = format_type or cfg.get("format", "human")
    truncate_max = cfg.get("truncate_query_max", DEFAULT_TRUNCATE_QUERY_MAX)

    root = logging.getLogger()
    if not force and root.handlers:
        return

    root.setLevel(getattr(logging, str(log_level).upper(), logging.INFO))

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(root.level)

    handler.addFilter(RequestIdFilter())

    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )

    if not root.handlers:
        root.addHandler(handler)
