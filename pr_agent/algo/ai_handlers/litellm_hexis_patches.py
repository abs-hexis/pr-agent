# =============================================================================
# HEXIS PATCH — runtime monkey-patches for litellm
# =============================================================================
# Loaded once at module import time by `litellm_ai_handler.py`.
#
# HISTORICAL CONTEXT:
#   litellm < 1.89 had a hard-coded whitelist
#       "effort='max' is only supported by Claude Opus 4.6"
#   so we monkey-patched `_apply_output_config` to also allow Opus 4.7/4.8.
#
# CURRENT STATE (litellm >= 1.89):
#   litellm switched to a DATA-DRIVEN validator that reads
#   `supports_max_reasoning_effort` from the model_prices JSON. Opus 4.6, 4.7,
#   AND 4.8 are all flagged true upstream → no monkey-patch needed.
#
# This module is kept as a no-op (with a defensive fallback patch for users
# who downgrade litellm) so the import path in `litellm_ai_handler.py` keeps
# working without churn. If we ever fully drop the < 1.89 fallback, this
# module can be deleted along with its single import.
# =============================================================================

from pr_agent.log import get_logger

# Module-level idempotency flag — DO NOT attach to the AnthropicConfig class
# itself: litellm iterates class attrs and would forward our flag as an
# `_HEXIS_PATCHED` field in the request body (Anthropic rejects it with 400).
_PATCHES_APPLIED = False


def _litellm_has_data_driven_effort_validator() -> bool:
    """True iff litellm uses `_supports_effort_level` for max-effort gating
    (added in 1.89 — see release notes / Anthropic transformation.py L347-359).
    """
    try:
        from litellm.llms.anthropic.chat.transformation import AnthropicConfig
        # The new validator is a static method introduced in 1.89.
        return hasattr(AnthropicConfig, "_validate_effort_for_model")
    except Exception:
        return False


def _apply_hexis_patches() -> None:
    """Apply runtime patches if litellm is too old to natively support 4.7/4.8.
    Idempotent.
    """
    global _PATCHES_APPLIED
    if _PATCHES_APPLIED:
        return

    if _litellm_has_data_driven_effort_validator():
        # litellm >= 1.89 already whitelists Opus 4.6/4.7/4.8 via its model
        # map; no patch needed.
        get_logger().info(
            "[hexis-patch] litellm >= 1.89 detected; native Opus 4.6/4.7/4.8 "
            "effort='max' support — skipping legacy AnthropicConfig patch."
        )
        _PATCHES_APPLIED = True
        return

    try:
        from litellm.llms.anthropic.chat.transformation import AnthropicConfig
    except Exception as e:
        get_logger().warning(
            f"[hexis-patch] Could not import AnthropicConfig — skipping patches: {e}"
        )
        return

    def patched_apply_output_config(self, data: dict, model: str, optional_params: dict) -> None:
        """Legacy fallback for litellm < 1.89: allow `effort='max'` on 4.6/4.7/4.8."""
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

        # HEXIS PATCH: also allow Opus 4.7 and 4.8 for `max` (Vertex AI accepts
        # them — verified via raw curl).
        if effort == "max" and not (
            self._is_opus_4_6_model(model)
            or self._is_opus_4_7_model(model)
            or "claude-opus-4-8" in model
        ):
            raise ValueError(
                f"effort='max' is only supported by Claude Opus 4.6, 4.7, or 4.8. "
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
        "[hexis-patch] Applied legacy AnthropicConfig._apply_output_config "
        "patch (litellm < 1.89 detected) — allows effort='max' on Opus 4.6/4.7/4.8."
    )


# Apply on import
_apply_hexis_patches()
