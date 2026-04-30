"""Bridge between PR-Agent's command dispatcher and our agentic reviewer.

PR-Agent's dispatcher (pr_agent/agent/pr_agent.py) instantiates a tool class
with `(pr_url, args=...)` and then awaits `tool.run()`. This adapter has
that exact shape but underneath calls `pr_agent.agentic.reviewer._do_review`
(or `review_pr_via_registry` for push-triggered runs).

It posts the resulting `review.md` markdown to the PR as a persistent comment
using the existing PRReviewHeader so updates replace the prior agentic review.
"""

from __future__ import annotations

import asyncio
import logging
import time
from functools import partial
from typing import Any

from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.algo.utils import PRReviewHeader
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import get_git_provider_with_context
from pr_agent.log import get_logger

# Import the agentic package
from pr_agent.agentic.config import load_settings
from pr_agent.agentic.github_app import (PRRef, PRNotAccessible,
                                          GitHubAPIError, parse_pr_url)
from pr_agent.agentic.registry import REGISTRY
from pr_agent.agentic.reviewer import _do_review


log = get_logger()


class PRReviewerAgentic:
    """Drop-in replacement for `PRReviewer` that uses Claude Opus 4.7 with
    repo-wide tool access for deeper reviews.
    """

    # Same constructor signature as PRReviewer so the dispatcher can swap us in
    def __init__(
        self,
        pr_url: str,
        is_answer: bool = False,
        is_auto: bool = False,
        args: list | None = None,
        ai_handler: partial[BaseAiHandler,] = LiteLLMAIHandler,
    ):
        self.pr_url = pr_url
        self.is_answer = is_answer
        self.is_auto = is_auto
        self.args = args or []
        # ai_handler is unused — agentic reviewer talks to LiteLLM directly.
        self.ai_handler = ai_handler

        # We'll need a git provider for posting the resulting comment + the
        # "Preparing review..." placeholder.
        self.git_provider = get_git_provider_with_context(pr_url)

    async def run(self) -> None:
        """Entry point invoked by the PR-Agent command dispatcher."""
        try:
            # Show a quick "preparing" placeholder so the PR shows we're working
            if get_settings().config.publish_output and not get_settings().config.get(
                'is_auto_command', False
            ):
                try:
                    self.git_provider.publish_comment(
                        "🤖 **Preparing agentic review** "
                        "(Claude Opus 4.7 with repo-wide tool access)... "
                        "this typically takes 1-3 minutes.",
                        is_temporary=True,
                    )
                except Exception as e:
                    log.warning(f"[agentic] Could not publish placeholder: {e}")

            pr = parse_pr_url(self.pr_url)
            settings = load_settings()

            # If there's already an in-flight agentic review for this PR
            # (e.g. from a previous synchronize event), cancel it so this
            # newer run takes over.
            head_sha_for_registry = ""
            try:
                from pr_agent.agentic.github_app import (
                    get_token_bundle, fetch_pr,
                )
                from pr_agent.agentic.config import load_github_app_private_key
                pk = load_github_app_private_key()
                bundle = get_token_bundle(
                    app_id=settings.github.app_id, private_key=pk,
                    owner=pr.owner, repo=pr.repo, base_url=settings.github.base_url,
                )
                pr_meta = fetch_pr(bundle.token, pr)
                head_sha_for_registry = (pr_meta.get("head") or {}).get("sha", "")
            except PRNotAccessible as e:
                log.error(f"[agentic] PR not accessible: {e}")
                self.git_provider.publish_comment(
                    f"❌ **Agentic review failed**: {e}", is_temporary=False,
                )
                self.git_provider.remove_initial_comment()
                return
            except Exception as e:
                log.warning(f"[agentic] Pre-flight head SHA lookup failed: {e}")

            log.info(
                f"[agentic] Running review for {pr.full_name}#{pr.number} "
                f"@ {head_sha_for_registry[:8]}"
            )

            async def _runner() -> None:
                try:
                    result = await _do_review(pr, settings)
                    if result.success and result.review_markdown:
                        await self._publish_review(result.review_markdown, result)
                    elif result.cancelled:
                        # In-flight cancellation; the newer task will publish.
                        log.info(f"[agentic] Cancelled, leaving prior placeholder")
                    else:
                        # Aborted (e.g. token-budget hit) — post a stub
                        msg = (
                            f"⚠️ **Agentic review did not complete**\n\n"
                            f"**Reason:** `{result.error or 'unknown'}`\n\n"
                            f"Try again, or fall back to `/review_classic` "
                            f"for the fast non-agentic review."
                        )
                        self.git_provider.publish_comment(msg, is_temporary=False)
                        self.git_provider.remove_initial_comment()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.exception(f"[agentic] Review crashed: {e}")
                    try:
                        self.git_provider.publish_comment(
                            f"❌ **Agentic review crashed**: `{e}`\n\n"
                            f"Try `/review_classic` for the fast fallback.",
                            is_temporary=False,
                        )
                        self.git_provider.remove_initial_comment()
                    except Exception:
                        pass

            # Use the registry so a new push during this review cancels us.
            await REGISTRY.start_or_replace(
                pr=pr,
                head_sha=head_sha_for_registry or "unknown",
                runner=_runner,
                replace_reason="new /review trigger (push or manual)",
            )

        except Exception as e:
            log.exception(f"[agentic] Top-level run() crashed: {e}")

    async def _publish_review(self, review_markdown: str, result: Any) -> None:
        """Post the final review.md as a PR comment.

        Uses publish_persistent_comment so subsequent /review runs UPDATE the
        same comment instead of stacking.
        """
        # Header so persistent_comment can find/replace the prior one
        header = f"{PRReviewHeader.REGULAR.value} 🔍 (agentic, Claude Opus 4.7 + max effort)"

        # Footer with run stats — useful for cost monitoring
        files_count = len(result.files_inspected or [])
        files_block = ""
        if files_count:
            files_list = "\n".join(f"- `{p}`" for p in result.files_inspected[:50])
            files_block = (
                f"\n\n<details><summary><strong>📂 {files_count} files inspected</strong></summary>\n\n"
                f"{files_list}\n\n</details>"
            )
        cost = f"${result.cost_estimate_usd:.2f}" if result.cost_estimate_usd else "n/a"
        footer = (
            f"\n\n---\n"
            f"<sub>🤖 Agentic review · "
            f"{result.duration_seconds:.0f}s · "
            f"{result.rounds} tool rounds · "
            f"{result.total_input_tokens:,} in / {result.total_output_tokens:,} out tokens · "
            f"~{cost}{files_block}</sub>"
        )

        body = f"{header}\n\n{review_markdown}{footer}"
        try:
            self.git_provider.publish_persistent_comment(
                body,
                initial_header=header,
                update_header=True,
                final_update_message=False,
            )
        except Exception as e:
            log.warning(f"[agentic] persistent_comment failed, falling back to publish_comment: {e}")
            self.git_provider.publish_comment(body, is_temporary=False)

        # Remove the temporary "preparing" placeholder
        try:
            self.git_provider.remove_initial_comment()
        except Exception:
            pass

        log.info(
            f"[agentic] Published review for {self.pr_url} "
            f"({len(body):,} chars, ~{cost})"
        )
