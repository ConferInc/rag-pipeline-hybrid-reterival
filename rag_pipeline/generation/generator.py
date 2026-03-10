from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

import yaml
from openai import OpenAI

from rag_pipeline.llm_retry import with_retry

logger = logging.getLogger(__name__)


def _load_llm_retry_config(config_path: str | Path = "embedding_config.yaml") -> dict[str, Any]:
    """Load llm_retry config from YAML."""
    path = Path(config_path)
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            raw = yaml.safe_load(f)
        return raw.get("llm_retry", {}) or {}
    except Exception:
        return {}


def _load_generation_config(config_path: str | Path = "embedding_config.yaml") -> dict[str, Any]:
    """Load generation config from YAML."""
    path = Path(config_path)
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            raw = yaml.safe_load(f)
        return raw.get("generation", {}) or {}
    except Exception:
        return {}


def generate_response(
    augmented_prompt: str,
    *,
    model: str | None = None,
    temperature: float = 0.7,
    max_tokens: int | None = None,
    config_path: str | Path = "embedding_config.yaml",
) -> str:
    """
    Send the augmented prompt to an LLM and return the response.

    Args:
        augmented_prompt: Full prompt with system + context + user query
        model: LLM model name (defaults to GENERATION_MODEL env var)
        temperature: Controls randomness (0 = deterministic, 1 = creative)
        max_tokens: Max tokens in response
        config_path: Path to embedding_config.yaml for llm_retry settings

    Returns:
        LLM response text
    """
    timeout = float(os.environ.get("LLM_TIMEOUT", "30"))
    client = OpenAI(
        base_url=os.environ.get("OPENAI_BASE_URL"),
        api_key=os.environ.get("OPENAI_API_KEY"),
        timeout=timeout,
    )

    gen_cfg = _load_generation_config(config_path)
    model = model or os.environ.get("GENERATION_MODEL") or gen_cfg.get("model", "openai/gpt-4o-mini")
    if max_tokens is None:
        env_max = os.environ.get("GENERATION_MAX_TOKENS")
        max_tokens = int(env_max) if env_max else gen_cfg.get("max_tokens", 1024)
    if "temperature" in gen_cfg:
        temperature = float(gen_cfg["temperature"])

    # Split augmented prompt into system and user parts
    system_content, user_content = _split_prompt(augmented_prompt)

    retry_cfg = _load_llm_retry_config(config_path)
    max_attempts = retry_cfg.get("max_attempts", 3)
    initial_delay_ms = retry_cfg.get("initial_delay_ms", 1000)
    max_delay_ms = retry_cfg.get("max_delay_ms", 30000)
    jitter = retry_cfg.get("jitter", True)

    def _call() -> Any:
        return client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_content},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )

    response = with_retry(
        _call,
        max_attempts=max_attempts,
        initial_delay_ms=float(initial_delay_ms),
        max_delay_ms=float(max_delay_ms),
        jitter=jitter,
    )

    content = response.choices[0].message.content if response.choices else None
    result = (content or "").strip()

    if not result:
        # Diagnose empty response
        choice = response.choices[0] if response.choices else None
        finish = getattr(choice, "finish_reason", None) if choice else None
        model_used = getattr(response, "model", "?")
        print(
            f"[DEBUG] Empty LLM response: model={model_used}, choices={len(response.choices or [])}, "
            f"finish_reason={finish!r}, content_type={type(content).__name__}",
            file=sys.stderr,
        )

    return result


def _split_prompt(augmented_prompt: str) -> tuple[str, str]:
    """
    Split the augmented prompt into system and user messages.

    Everything between [SYSTEM] and the next section becomes the system message.
    Everything else becomes the user message.
    """
    lines = augmented_prompt.split("\n")
    system_lines: list[str] = []
    user_lines: list[str] = []
    in_system = False

    for line in lines:
        if line.strip() == "[SYSTEM]":
            in_system = True
            continue
        elif line.strip().startswith("[") and line.strip().endswith("]") and in_system:
            in_system = False
            user_lines.append(line)
            continue

        if in_system:
            system_lines.append(line)
        else:
            user_lines.append(line)

    system = "\n".join(system_lines).strip()
    user = "\n".join(user_lines).strip()

    if not system:
        system = "You are a helpful nutrition assistant."

    return system, user
