"""Per-PR workspace management — concurrency-safe via bare-clone + git worktree.

Layout:
    agentic-reviews/repos/{owner}_{repo}.git/      <- bare clone (one per repo)
    agentic-reviews/runs/{run-id}/checkout/        <- per-run worktree

This makes parallel reviews of different PRs (or two reviews of the same PR
that briefly overlap) safe: each run has its own working tree even though
the underlying object database is shared.

Always `git fetch origin` into the bare BEFORE creating a worktree, so the
worktree sees the latest commits at the moment the review starts.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .github_app import PRRef


log = logging.getLogger("agentic.workspace")


# --------------------------------------------------------------------------- #
# Per-(owner,repo) async lock for first-time clone
# --------------------------------------------------------------------------- #
# Bug #1 fix: two webhooks for the same repo arriving concurrently must NOT
# race on `git clone --bare`. We hold a per-repo asyncio.Lock during the
# bootstrap; only the winner does the clone, others wait and find it ready.
_BARE_CLONE_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}


def _get_bare_clone_lock(owner: str, repo: str) -> asyncio.Lock:
    key = (owner, repo)
    if key not in _BARE_CLONE_LOCKS:
        _BARE_CLONE_LOCKS[key] = asyncio.Lock()
    return _BARE_CLONE_LOCKS[key]


# --------------------------------------------------------------------------- #
# Tiny git wrapper
# --------------------------------------------------------------------------- #

def _run(
    cmd: list[str],
    *,
    cwd: str | os.PathLike | None = None,
    timeout: int = 120,
    check: bool = True,
    retries: int = 0,
) -> subprocess.CompletedProcess:
    last_err: subprocess.CalledProcessError | None = None
    for attempt in range(retries + 1):
        try:
            return subprocess.run(
                cmd, cwd=cwd, capture_output=True, text=True,
                check=check, timeout=timeout,
            )
        except subprocess.CalledProcessError as e:
            last_err = e
            if attempt < retries:
                wait = 2 ** attempt
                log.warning(
                    "git command failed (attempt %d/%d), retrying in %ds: %s\n  stderr: %s",
                    attempt + 1, retries + 1, wait, " ".join(cmd[:3]) + "...",
                    (e.stderr or "").strip()[:200],
                )
                time.sleep(wait)
        except subprocess.TimeoutExpired as e:
            log.error("git command timed out after %ds: %s", timeout, " ".join(cmd[:3]))
            raise
    assert last_err is not None
    raise last_err


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def bare_repo_dir(repos_dir: Path, pr: PRRef) -> Path:
    """Path to the bare clone (.git dir) for {owner}/{repo}."""
    return repos_dir / f"{pr.owner}_{pr.repo}.git"


async def ensure_bare_clone(
    *, repos_dir: Path, pr: PRRef, clone_url: str,
) -> Path:
    """Make sure the bare clone exists. Returns its path.

    Idempotent and concurrency-safe (Bug #1 fix): two webhooks for the same
    repo arriving simultaneously will serialize through a per-(owner,repo)
    asyncio.Lock so only one tries to clone.

    Existing bare clones are left as-is (we just refresh the remote URL with
    the freshly-minted token).
    """
    repos_dir.mkdir(parents=True, exist_ok=True)
    bare = bare_repo_dir(repos_dir, pr)

    lock = _get_bare_clone_lock(pr.owner, pr.repo)
    async with lock:
        if not bare.exists():
            log.info("First-time bare clone of %s -> %s", pr.full_name, bare)
            # Use a temp dir then atomically rename, so a partial clone never
            # leaves a half-finished `bare` dir to mislead a future check.
            tmp = bare.with_suffix(".git.tmp")
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
            try:
                _run([
                    "git", "clone", "--bare", "--filter=blob:none",
                    clone_url, str(tmp),
                ], timeout=600)
                # Configure refspec so future fetches pick up PR refs
                _run([
                    "git", "config", "--add", "remote.origin.fetch",
                    "+refs/pull/*/head:refs/remotes/origin/pr/*",
                ], cwd=tmp)
                tmp.rename(bare)
            except Exception:
                shutil.rmtree(tmp, ignore_errors=True)
                raise
        else:
            # Refresh token in the remote URL
            _run(["git", "remote", "set-url", "origin", clone_url], cwd=bare)
        # Bug #14 hygiene: prune any leaked worktree refs from previous SIGKILLs
        try:
            _run(["git", "worktree", "prune"], cwd=bare, timeout=10, check=False)
        except Exception:
            pass
    return bare


def fetch_pr_into_bare(
    *, bare: Path, pr_number: int, head_sha: str,
    base_branch: str | None = None,
) -> None:
    """Fetch the latest commits we need (PR ref + head SHA + base branch)
    into the bare repo. Best-effort: at least one of these must succeed.

    Bug #18 fix: PR ref may not exist on GitHub yet for a few seconds after
    `pull_request.opened` fires. We retry with longer backoff on the
    "couldn't find remote ref" / 404 case (5s, 10s, 20s).
    """
    pr_ref = f"refs/pull/{pr_number}/head"
    fetched_pr = False
    last_err: subprocess.CalledProcessError | None = None
    backoffs = [0, 5, 10, 20]   # 4 attempts, growing delays
    for attempt, wait in enumerate(backoffs):
        if wait:
            log.info("Retrying fetch of %s after %ds (attempt %d/%d)",
                     pr_ref, wait, attempt + 1, len(backoffs))
            time.sleep(wait)
        try:
            _run([
                "git", "fetch", "--prune", "--depth=200", "origin",
                f"{pr_ref}:refs/remotes/origin/pr/{pr_number}",
            ], cwd=bare, timeout=120)
            fetched_pr = True
            break
        except subprocess.CalledProcessError as e:
            last_err = e
            err_text = ((e.stderr or "") + (e.stdout or "")).lower()
            transient = (
                "couldn't find remote ref" in err_text
                or "404" in err_text
                or "not our ref" in err_text
                or "did not send all necessary objects" in err_text
            )
            if not transient and attempt > 0:
                # Permanent failure (auth, network, etc.) — stop retrying
                log.warning("fetch %s failed permanently: %s",
                            pr_ref, (e.stderr or "").strip()[:200])
                break

    if not fetched_pr:
        log.warning("Could not fetch %s after %d attempts, will try SHA: %s",
                    pr_ref, len(backoffs),
                    (last_err.stderr if last_err else "").strip()[:200])

    # Always also fetch the SHA directly — defends against rebase/amend cases
    # where the PR ref's HEAD is newer than what we want, and against the
    # PR ref being unavailable.
    try:
        _run([
            "git", "fetch", "--depth=200", "origin", head_sha,
        ], cwd=bare, timeout=120, retries=1)
    except subprocess.CalledProcessError as e:
        if not fetched_pr:
            raise RuntimeError(
                f"Could not fetch either {pr_ref} or {head_sha}: "
                f"{(e.stderr or '').strip()}"
            ) from e
        # If we already have the PR ref, the SHA is reachable from it.
        log.debug("Could not fetch SHA %s directly, but PR ref worked.", head_sha[:8])

    # Best-effort: fetch base branch too (for git-blame / diff comparisons)
    if base_branch:
        try:
            _run([
                "git", "fetch", "--depth=50", "origin",
                f"{base_branch}:refs/remotes/origin/{base_branch}",
            ], cwd=bare, timeout=60, retries=1)
        except subprocess.CalledProcessError as e:
            log.debug("Optional base-branch fetch failed (ignored): %s",
                      (e.stderr or "").strip()[:120])


def add_worktree(*, bare: Path, worktree_dir: Path, head_sha: str) -> Path:
    """Create a per-run git worktree at worktree_dir, checked out at head_sha.

    Worktrees are independent working dirs sharing the bare's object db. Two
    parallel runs of the same repo each get their own worktree, no races on
    `git checkout`.
    """
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)
    if worktree_dir.exists():
        # Shouldn't happen with our run-id naming, but defend anyway
        shutil.rmtree(worktree_dir)
    _run([
        "git", "worktree", "add", "--detach",
        str(worktree_dir), head_sha,
    ], cwd=bare, timeout=60)
    return worktree_dir


def remove_worktree(*, bare: Path, worktree_dir: Path) -> None:
    """Remove a per-run worktree. Idempotent."""
    if not worktree_dir.exists():
        return
    try:
        _run([
            "git", "worktree", "remove", "--force", str(worktree_dir),
        ], cwd=bare, timeout=30, check=False)
    except Exception as e:
        log.debug("git worktree remove failed (will rmtree): %s", e)
    if worktree_dir.exists():
        shutil.rmtree(worktree_dir, ignore_errors=True)
    # Prune dangling worktree metadata
    try:
        _run(["git", "worktree", "prune"], cwd=bare, timeout=10, check=False)
    except Exception:
        pass


@contextmanager
def per_run_worktree(
    *, bare: Path, worktree_dir: Path, head_sha: str,
) -> Iterator[Path]:
    """Context manager: create a worktree, yield its path, remove on exit."""
    add_worktree(bare=bare, worktree_dir=worktree_dir, head_sha=head_sha)
    try:
        yield worktree_dir
    finally:
        remove_worktree(bare=bare, worktree_dir=worktree_dir)


# --------------------------------------------------------------------------- #
# Path safety (unchanged from previous version)
# --------------------------------------------------------------------------- #

def safe_join(repo_root: Path, user_path: str) -> Path:
    """Resolve user_path against repo_root; refuse if it escapes the workspace."""
    if not isinstance(user_path, str) or not user_path:
        raise ValueError("path must be a non-empty string")
    user_path = user_path.lstrip("/")
    candidate = (repo_root / user_path).resolve()
    root = repo_root.resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ValueError(
            f"path {user_path!r} escapes the repo workspace {root}"
        )
    return candidate


# --------------------------------------------------------------------------- #
# Maintenance
# --------------------------------------------------------------------------- #

def cleanup_old_runs(
    runs_dir: Path, *, ttl_days: int = 30,
    throttle_seconds: int = 24 * 3600,
) -> int:
    """Delete run directories older than ttl_days. Returns count removed.

    Bug #13 fix: throttled to once per `throttle_seconds` (default 24h) via a
    marker file, so even a flurry of reviews doesn't waste cycles re-scanning
    the runs directory.
    """
    if not runs_dir.exists():
        return 0

    marker = runs_dir / ".last-cleanup"
    if marker.exists():
        try:
            last = marker.stat().st_mtime
            if (time.time() - last) < throttle_seconds:
                log.debug("Skipping cleanup (last ran %.0fs ago, throttle=%ds)",
                          time.time() - last, throttle_seconds)
                return 0
        except OSError:
            pass

    cutoff = time.time() - ttl_days * 86400
    removed = 0
    for child in runs_dir.iterdir():
        if not child.is_dir():
            continue
        try:
            mtime = child.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            try:
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
            except Exception as e:
                log.warning("Could not remove old run dir %s: %s", child, e)

    # Touch the marker (always, so we don't repeatedly scan even when nothing
    # was removed)
    try:
        marker.touch()
    except OSError as e:
        log.debug("Could not touch cleanup marker: %s", e)

    if removed:
        log.info("[cleanup] removed %d run dirs older than %d days", removed, ttl_days)
    return removed


def cleanup_runs_for_pr(runs_dir: Path, pr: PRRef) -> int:
    """Delete all run dirs for a specific PR.

    Called when we get a `pull_request.closed` webhook so disk doesn't bloat
    indefinitely after a PR is merged.
    """
    if not runs_dir.exists():
        return 0
    prefix = f"{pr.owner}_{pr.repo}_pr-{pr.number}_"
    removed = 0
    for child in runs_dir.iterdir():
        if child.is_dir() and child.name.startswith(prefix):
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
    log.info("[cleanup] removed %d run dirs for closed PR %s", removed, pr.html_url)
    return removed
