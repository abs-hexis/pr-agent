"""Registry of in-flight reviews — used to coordinate cancellation.

Behavior contract:
- One review at a time per (owner, repo, pr_number) key.
- A new push (commit-changing event) for an already-running PR cancels the
  in-flight task and starts a fresh one.
- A new comment on an already-running PR is IGNORED — let the in-flight
  review finish; the user can re-trigger via @mention or /review_deep if they
  want a fresh take that includes their new comment.
- Webhook for `pull_request.closed`: cancel any in-flight task for the PR
  and clean up disk via cleanup_runs_for_pr().

This module is async-safe (uses asyncio.Task / asyncio.Lock) but NOT
process-safe — we run a single FastAPI worker for the agentic path so this
is fine. If we ever scale to multiple workers, we'd need an external lock
(redis / file).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from .github_app import PRRef


log = logging.getLogger("agentic.registry")


@dataclass
class InFlight:
    pr: PRRef
    task: asyncio.Task
    started_at: float
    head_sha: str        # the SHA we're CURRENTLY reviewing


class ReviewRegistry:
    """Process-local registry of in-flight reviews."""

    def __init__(self) -> None:
        self._tasks: dict[tuple[str, str, int], InFlight] = {}
        self._lock = asyncio.Lock()

    def _key(self, pr: PRRef) -> tuple[str, str, int]:
        return (pr.owner, pr.repo, pr.number)

    async def get(self, pr: PRRef) -> InFlight | None:
        async with self._lock:
            return self._tasks.get(self._key(pr))

    async def start_or_replace(
        self,
        pr: PRRef,
        head_sha: str,
        runner: Callable[[], Awaitable[None]],
        *,
        replace_reason: str = "new commit",
    ) -> InFlight:
        """Start a new review for pr@head_sha, cancelling any existing one.

        `runner` is the zero-arg async callable we await. We wrap it in a Task
        so we can cancel it.
        """
        import time
        async with self._lock:
            key = self._key(pr)
            existing = self._tasks.get(key)
            if existing:
                log.info(
                    "[registry] %s: cancelling in-flight review (was @%s, now @%s) — %s",
                    pr.full_name + f"#{pr.number}",
                    existing.head_sha[:8], head_sha[:8], replace_reason,
                )
                existing.task.cancel()
                try:
                    await existing.task
                except (asyncio.CancelledError, Exception):
                    pass

            task = asyncio.create_task(runner())
            inflight = InFlight(
                pr=pr, task=task,
                started_at=time.time(),
                head_sha=head_sha,
            )
            self._tasks[key] = inflight

            # Auto-remove from registry when task completes (success or fail)
            def _on_done(_t: asyncio.Task, _key=key) -> None:
                # NB: callback fires on event loop, not under our lock — but
                # dict pop is atomic in CPython, so this is safe enough.
                # If the slot has been replaced by a newer task, leave it alone.
                cur = self._tasks.get(_key)
                if cur is not None and cur.task is _t:
                    self._tasks.pop(_key, None)
            task.add_done_callback(_on_done)

            return inflight

    async def is_active(self, pr: PRRef) -> bool:
        existing = await self.get(pr)
        return existing is not None and not existing.task.done()

    async def cancel(self, pr: PRRef, *, reason: str = "explicit cancel") -> bool:
        """Cancel an in-flight review for `pr`. Returns True if one was running."""
        async with self._lock:
            key = self._key(pr)
            existing = self._tasks.pop(key, None)
        if not existing:
            return False
        log.info(
            "[registry] %s: cancelled — %s",
            pr.full_name + f"#{pr.number}", reason,
        )
        existing.task.cancel()
        try:
            await existing.task
        except (asyncio.CancelledError, Exception):
            pass
        return True

    def snapshot(self) -> list[InFlight]:
        """Non-blocking snapshot of in-flight reviews (for status endpoints)."""
        return list(self._tasks.values())


# Module-level singleton (kept simple — only one of these per process)
REGISTRY = ReviewRegistry()
