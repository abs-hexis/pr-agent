"""LiteLLM wrapper that handles tool-calling against Vertex AI Claude Opus 4.7.

Mirrors the litellm runtime monkey-patch from the production webhook server,
so we can use `output_config.effort = "max"` even though stock litellm
rejects it for Opus 4.7.
"""

from __future__ import annotations

import logging
from typing import Any

import litellm

log = logging.getLogger("agentic.llm")


# --------------------------------------------------------------------------- #
# Runtime monkey-patch (same as production litellm_hexis_patches.py)
# --------------------------------------------------------------------------- #

_PATCHED = False


def apply_hexis_litellm_patches() -> None:
    """Allow output_config.effort='max' on Claude Opus 4.7."""
    global _PATCHED
    if _PATCHED:
        return
    try:
        from litellm.llms.anthropic.chat.transformation import AnthropicConfig
    except Exception as e:
        log.warning("Could not import AnthropicConfig — running without HEXIS patch: %s", e)
        return

    def patched_apply_output_config(self, data: dict, model: str, optional_params: dict) -> None:
        if "output_config" not in optional_params:
            return
        oc = optional_params.get("output_config")
        if not oc or not isinstance(oc, dict):
            return
        effort = oc.get("effort")
        valid = {"high", "medium", "low", "xhigh", "max"}
        if effort and effort not in valid:
            raise ValueError(f"Invalid effort: {effort}. Must be one of {sorted(valid)}")
        # HEXIS PATCH: also allow Opus 4.7 for `max`
        if effort == "max" and not (
            self._is_opus_4_6_model(model) or self._is_opus_4_7_model(model)
        ):
            raise ValueError(
                f"effort='max' is only supported by Claude Opus 4.6/4.7. Got: {model}"
            )
        if effort == "xhigh" and not self._supports_effort_level(model, "xhigh"):
            raise ValueError(f"effort='xhigh' not supported by {model}")
        data["output_config"] = oc

    AnthropicConfig._apply_output_config = patched_apply_output_config
    _PATCHED = True
    log.info("[hexis] Patched litellm AnthropicConfig._apply_output_config")


# --------------------------------------------------------------------------- #
# Tool-calling completion
# --------------------------------------------------------------------------- #

# Anthropic Vertex AI hard caps for `max_tokens` per model.
# Updated: 2026 — Opus 4.6/4.7 support up to 64K output tokens.
_MAX_TOKENS_CAPS = {
    "claude-opus-4-7": 64_000,
    "claude-opus-4-6": 64_000,
    # newer/older models can be added here
}


def _model_max_tokens_cap(model: str) -> int | None:
    for key, cap in _MAX_TOKENS_CAPS.items():
        if key in model:
            return cap
    return None


async def acompletion_with_tools(
    *,
    model: str,
    messages: list[dict],
    tools: list[dict],
    vertex_project: str,
    vertex_location: str,
    effort: str = "max",
    max_tokens: int = 16000,
    timeout: int = 120,
    extra_kwargs: dict[str, Any] | None = None,
) -> Any:
    """Call LiteLLM with tools enabled. Returns raw response object.

    For Anthropic Opus 4.6/4.7 we layer in the adaptive-thinking schema:
        thinking={"type":"adaptive"} + output_config={"effort": effort}

    `max_tokens` is clamped to the model's published ceiling (Opus 4.x = 64K).

    Bug #12 fix: on 429 / RateLimitError we retry with exponential backoff
    (5s, 15s, 45s) before giving up.
    """
    import asyncio
    apply_hexis_litellm_patches()

    # Clamp max_tokens to the model's hard cap
    cap = _model_max_tokens_cap(model)
    effective_max_tokens = max_tokens
    if cap is not None and max_tokens > cap:
        log.warning(
            "max_tokens=%d exceeds model %s cap of %d; clamping",
            max_tokens, model, cap,
        )
        effective_max_tokens = cap

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "temperature": 1,    # required when adaptive thinking is on
        "max_tokens": effective_max_tokens,
        "timeout": timeout,
        "vertex_project": vertex_project,
        "vertex_location": vertex_location,
    }

    if "claude-opus-4-7" in model or "claude-opus-4-6" in model:
        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["output_config"] = {"effort": effort}

    if extra_kwargs:
        kwargs.update(extra_kwargs)

    # Detect rate-limit-like errors so we can retry. We probe by name AND
    # by status code so we work whether litellm raises its own exception
    # type or the underlying provider's.
    backoffs = [5, 15, 45]
    last_err: Exception | None = None
    for attempt, wait in enumerate([0] + backoffs):
        if wait:
            log.warning(
                "Rate-limited or transient LLM error; sleeping %ds then retry %d/%d",
                wait, attempt, len(backoffs),
            )
            await asyncio.sleep(wait)
        try:
            return await litellm.acompletion(**kwargs)
        except Exception as e:
            last_err = e
            # Heuristic: rate-limit-y? retry. Otherwise, raise.
            err_name = type(e).__name__.lower()
            err_msg = str(e).lower()
            is_rate_limit = (
                "ratelimit" in err_name
                or "rate limit" in err_msg
                or "429" in err_msg
                or "resourceexhausted" in err_name
                or "quota" in err_msg
            )
            is_transient_5xx = (
                "internalservererror" in err_name
                or "503" in err_msg
                or "502" in err_msg
                or "504" in err_msg
                or "overloaded" in err_msg
            )
            if not (is_rate_limit or is_transient_5xx):
                raise
            if attempt >= len(backoffs):
                # Out of retries
                log.error("Out of retries after %d rate-limit attempts", len(backoffs))
                raise
    # Unreachable, but appease the type checker
    assert last_err is not None
    raise last_err
