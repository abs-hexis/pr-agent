# =============================================================================
# HEXIS PATCH — runtime monkey-patches for litellm
# =============================================================================
# Loaded once at module import time by `litellm_ai_handler.py`.
#
# Why monkey-patch instead of forking litellm?
#   - litellm pins itself with the `==` operator in its installer, and a fork
#     means we have to maintain compatibility every release.
#   - The bug we fix is purely client-side validation; the actual Vertex AI
#     endpoint accepts our payload (we proved this with raw curl).
#
# Patch 1: allow `output_config.effort = "max"` on Claude Opus 4.7
#   litellm's `AnthropicConfig._apply_output_config` raises:
#       "effort='max' is only supported by Claude Opus 4.6"
#   …but the Vertex AI endpoint actually returns 200 OK for Opus 4.7 + max.
#   We override the validator to also allow Opus 4.7.
# =============================================================================

from pr_agent.log import get_logger

# Module-level idempotency flag — DO NOT attach to the AnthropicConfig class
# itself: litellm iterates class attrs and would forward our flag as an
# `_HEXIS_PATCHED` field in the request body (Anthropic rejects it with 400).
_PATCHES_APPLIED = False


def _apply_hexis_patches() -> None:
    """Apply all HEXIS runtime patches to litellm. Idempotent."""
    global _PATCHES_APPLIED
    if _PATCHES_APPLIED:
        return

    try:
        from litellm.llms.anthropic.chat.transformation import AnthropicConfig
    except Exception as e:
        get_logger().warning(f"[hexis-patch] Could not import AnthropicConfig — skipping patches: {e}")
        return

    original_apply = AnthropicConfig._apply_output_config

    def patched_apply_output_config(self, data: dict, model: str, optional_params: dict) -> None:
        """Allow `effort='max'` for both Opus 4.6 AND Opus 4.7.

        Vertex AI accepts `output_config.effort=max` for opus-4-7 (verified via
        raw curl) — only litellm's client-side check rejects it. We swap in our
        own validator that whitelists 4.7 in addition to 4.6.
        """
        if "output_config" not in optional_params:
            return
        output_config = optional_params.get("output_config")
        if not output_config or not isinstance(output_config, dict):
            return

        effort = output_config.get("effort")
        valid_efforts = ["high", "medium", "low", "xhigh", "max"]
        if effort and effort not in valid_efforts:
            raise ValueError(
                f"Invalid effort value: {effort}. Must be one of: "
                f"'high', 'medium', 'low', 'xhigh', 'max'"
            )

        # HEXIS PATCH: also allow Opus 4.7 for `max`
        if effort == "max" and not (
            self._is_opus_4_6_model(model) or self._is_opus_4_7_model(model)
        ):
            raise ValueError(
                f"effort='max' is only supported by Claude Opus 4.6 or 4.7. "
                f"Got model: {model}"
            )

        # `xhigh` is data-driven via `supports_xhigh_reasoning_effort`
        if effort == "xhigh" and not self._supports_effort_level(model, "xhigh"):
            raise ValueError(
                f"effort='xhigh' is not supported by this model. Got model: {model}"
            )

        data["output_config"] = output_config

    AnthropicConfig._apply_output_config = patched_apply_output_config
    _PATCHES_APPLIED = True

    get_logger().info(
        "[hexis-patch] Patched litellm AnthropicConfig._apply_output_config "
        "to allow effort='max' on Claude Opus 4.7"
    )


# Apply on import
_apply_hexis_patches()
