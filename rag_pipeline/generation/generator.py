from __future__ import annotations

import os
import sys

from openai import OpenAI


def generate_response(
    augmented_prompt: str,
    *,
    model: str | None = None,
    temperature: float = 0.7,
    max_tokens: int | None = None,
) -> str:
    """
    Send the augmented prompt to an LLM and return the response.

    Args:
        augmented_prompt: Full prompt with system + context + user query
        model: LLM model name (defaults to GENERATION_MODEL env var)
        temperature: Controls randomness (0 = deterministic, 1 = creative)
        max_tokens: Max tokens in response

    Returns:
        LLM response text
    """
    client = OpenAI(
        base_url=os.environ.get("OPENAI_BASE_URL"),
        api_key=os.environ.get("OPENAI_API_KEY"),
    )
    model = model or os.environ.get("GENERATION_MODEL", "openai/gpt-5-mini")
    if max_tokens is None:
        max_tokens = int(os.environ.get("GENERATION_MAX_TOKENS", "2048"))

    # Split augmented prompt into system and user parts
    system_content, user_content = _split_prompt(augmented_prompt)

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
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
