"""Command-line entry-points for the agentic reviewer.

Subcommands:
  review <pr-url>           Run an agentic review of a PR
  cleanup <pr-url>          Delete all run dirs for a PR (e.g. after PR closed)
  list-runs <pr-url>        Show past runs for a PR (paths + meta summary)
  status                    Show in-flight reviews (only meaningful in long-lived mode)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from .config import load_settings
from .github_app import parse_pr_url
from .registry import REGISTRY
from .reviewer import _setup_logging, review_pr
from .workspace import (bare_repo_dir, cleanup_old_runs, cleanup_runs_for_pr)


log = logging.getLogger("agentic.cli")


def _list_pr_runs(runs_dir: Path, pr_owner: str, pr_repo: str, pr_number: int) -> list[Path]:
    if not runs_dir.exists():
        return []
    prefix = f"{pr_owner}_{pr_repo}_pr-{pr_number}_"
    return sorted([p for p in runs_dir.iterdir() if p.is_dir() and p.name.startswith(prefix)])


# --------------------------------------------------------------------------- #
# Subcommand handlers
# --------------------------------------------------------------------------- #

def cmd_review(args: argparse.Namespace) -> int:
    try:
        result = asyncio.run(review_pr(args.pr_url))
        return 0 if result.success else 1
    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)
        return 130


def cmd_cleanup(args: argparse.Namespace) -> int:
    settings = load_settings()
    pr = parse_pr_url(args.pr_url)
    runs = _list_pr_runs(settings.runs_dir, pr.owner, pr.repo, pr.number)
    if not runs:
        print(f"(no runs to clean up for {pr.full_name}#{pr.number})")
        return 0
    print(f"Will delete {len(runs)} run dirs for {pr.full_name}#{pr.number}:")
    for r in runs:
        print(f"  - {r.name}")
    if not args.yes:
        confirm = input("Proceed? [y/N] ").strip().lower()
        if confirm not in ("y", "yes"):
            print("Aborted.")
            return 1
    removed = cleanup_runs_for_pr(settings.runs_dir, pr)
    print(f"✅ Removed {removed} run dirs.")
    if args.also_repo:
        bare = bare_repo_dir(settings.repos_dir, pr)
        if bare.exists():
            import shutil
            shutil.rmtree(bare, ignore_errors=True)
            print(f"✅ Also removed bare clone: {bare}")
        else:
            print(f"(no bare clone at {bare})")
    return 0


def cmd_list_runs(args: argparse.Namespace) -> int:
    settings = load_settings()
    pr = parse_pr_url(args.pr_url)
    runs = _list_pr_runs(settings.runs_dir, pr.owner, pr.repo, pr.number)
    if not runs:
        print(f"(no runs for {pr.full_name}#{pr.number})")
        return 0
    print(f"Runs for {pr.full_name}#{pr.number}:")
    for r in runs:
        meta_path = r / "meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                status = "🛑" if meta.get("cancelled") else (
                    "⚠" if meta.get("abort_reason") else "✅"
                )
                summary = (
                    f"  {status} {r.name}  "
                    f"({meta.get('rounds', 0)} rounds, "
                    f"{meta.get('files_inspected_count', 0)} files, "
                    f"{meta.get('duration_seconds', '?')}s)"
                )
                if meta.get("abort_reason"):
                    summary += f"  reason: {meta['abort_reason'][:60]}"
                print(summary)
            except Exception:
                print(f"  ?  {r.name}  (could not read meta)")
        else:
            print(f"  ?  {r.name}  (no meta.json)")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    snap = REGISTRY.snapshot()
    if not snap:
        print("(no in-flight reviews)")
        return 0
    print(f"In-flight reviews ({len(snap)}):")
    import time
    now = time.time()
    for f in snap:
        elapsed = now - f.started_at
        print(f"  {f.pr.full_name}#{f.pr.number}  @{f.head_sha[:8]}  "
              f"({elapsed:.0f}s elapsed, done={f.task.done()})")
    return 0


def cmd_cleanup_old(args: argparse.Namespace) -> int:
    """Run TTL-based cleanup of all old run dirs (any PR)."""
    settings = load_settings()
    removed = cleanup_old_runs(settings.runs_dir, ttl_days=args.ttl_days)
    print(f"✅ Removed {removed} run dirs older than {args.ttl_days} days.")
    return 0


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agentic-cli",
        description="Local-mode agentic PR reviewer powered by Claude Opus 4.7",
    )
    p.add_argument("-v", "--verbose", action="count", default=1)
    sub = p.add_subparsers(dest="cmd", required=True)

    pr_review = sub.add_parser("review", help="Run an agentic review of a PR")
    pr_review.add_argument("pr_url", help="PR URL or owner/repo#N")
    pr_review.set_defaults(func=cmd_review)

    pr_cleanup = sub.add_parser(
        "cleanup", help="Delete all run dirs for a PR (use after PR is merged/closed)"
    )
    pr_cleanup.add_argument("pr_url", help="PR URL or owner/repo#N")
    pr_cleanup.add_argument("-y", "--yes", action="store_true", help="Skip confirmation")
    pr_cleanup.add_argument(
        "--also-repo", action="store_true",
        help="Also delete the cached bare clone of the entire repo",
    )
    pr_cleanup.set_defaults(func=cmd_cleanup)

    pr_list = sub.add_parser("list-runs", help="Show past runs for a PR")
    pr_list.add_argument("pr_url", help="PR URL or owner/repo#N")
    pr_list.set_defaults(func=cmd_list_runs)

    pr_status = sub.add_parser("status", help="Show in-flight reviews (long-lived mode only)")
    pr_status.set_defaults(func=cmd_status)

    pr_cleanup_old = sub.add_parser(
        "cleanup-old", help="Delete run dirs older than N days (default 30)"
    )
    pr_cleanup_old.add_argument("--ttl-days", type=int, default=30)
    pr_cleanup_old.set_defaults(func=cmd_cleanup_old)

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    _setup_logging(args.verbose)
    try:
        return args.func(args)
    except Exception as e:
        log.exception("Fatal error: %s", e)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
