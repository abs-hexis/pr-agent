"""The agentic-review loop driver.

Concurrency model:
- One **bare clone** per repo, shared across runs:
    agentic-reviews/repos/{owner}_{repo}.git
- One **per-run git worktree**, isolated:
    agentic-reviews/runs/{run-id}/checkout/

Two parallel reviews of the same repo each get their own worktree, no race
on `git checkout`.

Lifecycle:
- New review for a PR cancels any in-flight review for that PR (registry).
- A new comment during review does NOT cancel — let the in-flight finish.
- `pull_request.closed` webhook → cancel + cleanup_runs_for_pr().

Per-run artifacts written to runs/{run-id}/:
    review.md          ← final markdown
    tool_log.jsonl     ← every tool call + result (audit)
    transcript.md      ← full LLM conversation (debug)
    meta.json          ← timings + token use + rails
    checkout/          ← the git worktree for this run (cleaned up at exit)
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Bug #3: cap each `tool` message's content to this many chars before
# appending to the conversation. Belt-and-braces in case a future tool
# returns more than its own internal cap (e.g. read_files batch).
MAX_TOOL_RESULT_CHARS = 150_000

from .config import Settings, load_github_app_private_key, load_settings
from .github_app import (PRRef, TokenBundle, fetch_pr, fetch_pr_comments,
                         fetch_pr_diff, fetch_pr_files, get_token_bundle,
                         parse_pr_url)
from .llm import acompletion_with_tools
from .prompt import SYSTEM_PROMPT, render_user_prompt
from .registry import REGISTRY
from .tools import TOOL_HANDLERS, ToolContext, get_tool_specs
from .workspace import (cleanup_old_runs, ensure_bare_clone, fetch_pr_into_bare,
                        per_run_worktree)


log = logging.getLogger("agentic.reviewer")


@dataclass
class ReviewResult:
    success: bool
    review_markdown: str
    error: str | None = None
    rounds: int = 0
    files_inspected: list[str] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    cost_estimate_usd: float | None = None
    duration_seconds: float = 0.0
    run_dir: Path | None = None
    cancelled: bool = False


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _new_run_dir(runs_dir: Path, pr: PRRef) -> Path:
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = runs_dir / f"{pr.owner}_{pr.repo}_pr-{pr.number}_{ts}"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _append_jsonl(path: Path, obj: dict) -> None:
    with path.open("a") as fh:
        fh.write(json.dumps(obj, default=str) + "\n")


def _approx_message_tokens(messages: list[dict]) -> int:
    """Cheap estimate (chars/4) — good enough for budget tracking."""
    n = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            n += len(c)
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict):
                    n += len(part.get("text", ""))
        # tool_calls payloads
        v = m.get("tool_calls")
        if v:
            n += len(json.dumps(v))
    return n // 4


def _estimate_cost(input_tokens: int, output_tokens: int, model: str) -> float | None:
    """Anthropic Opus 4.x on Vertex (as of 2026): $15/M input, $75/M output."""
    if "opus-4-7" in model or "opus-4-6" in model:
        return (input_tokens * 15 / 1_000_000) + (output_tokens * 75 / 1_000_000)
    return None


# --------------------------------------------------------------------------- #
# The review function (public API)
# --------------------------------------------------------------------------- #

async def review_pr(
    pr_url: str,
    *,
    settings: Settings | None = None,
    use_registry: bool = False,
) -> ReviewResult:
    """Run an agentic review of a PR.

    If `use_registry=True`, registers this run in the in-flight registry so
    a future call for the same PR can cancel it. (Local CLI usage stays
    `use_registry=False`; production webhook integration will set it True.)
    """
    settings = settings or load_settings()
    pr = parse_pr_url(pr_url)

    if use_registry:
        # Wrap the actual run in the registry — caller gets back a future
        # they can ignore, or we can do it here synchronously.
        # For simplicity right now, just delegate to _do_review directly;
        # webhook server will use REGISTRY.start_or_replace itself.
        return await _do_review(pr, settings)

    return await _do_review(pr, settings)


async def _do_review(pr: PRRef, settings: Settings) -> ReviewResult:
    """The real review — separated so registry.start_or_replace() can wrap it."""
    started = time.time()
    # Periodic cleanup of old runs (cheap, runs at start of each new review)
    try:
        cleanup_old_runs(settings.runs_dir, ttl_days=30)
    except Exception as e:
        log.warning("cleanup_old_runs failed (ignored): %s", e)

    print(f"▶ Reviewing {pr.html_url}")

    # ---- Auth ----
    if not settings.github.app_id:
        raise RuntimeError(
            "GITHUB.APP_ID not set in env (load .env via load_settings first)"
        )
    private_key = load_github_app_private_key()
    bundle = get_token_bundle(
        app_id=settings.github.app_id, private_key=private_key,
        owner=pr.owner, repo=pr.repo, base_url=settings.github.base_url,
    )
    print(f"▶ Authenticated as GitHub App (installation {bundle.installation_id})")

    # ---- Fetch PR data ----
    bundle.refresh_if_stale()
    pr_meta = fetch_pr(bundle.token, pr)
    head_sha = (pr_meta.get("head") or {}).get("sha", "")
    base_branch = (pr_meta.get("base") or {}).get("ref")
    if not head_sha:
        raise RuntimeError(f"Could not determine head SHA for PR #{pr.number}")

    print(f"▶ PR #{pr.number}: '{pr_meta.get('title')}' "
          f"— head={head_sha[:8]} (base={base_branch})")

    # ---- Workspace: bare clone + fetch + per-run worktree ----
    bare = await ensure_bare_clone(
        repos_dir=settings.repos_dir, pr=pr, clone_url=bundle.clone_url,
    )
    fetch_pr_into_bare(
        bare=bare, pr_number=pr.number, head_sha=head_sha,
        base_branch=base_branch,
    )

    # ---- Fetch other PR data while we have a token ----
    bundle.refresh_if_stale()
    pr_diff = fetch_pr_diff(bundle.token, pr)
    issue_comments, review_comments = fetch_pr_comments(bundle.token, pr)
    pr_comments = {"issue": issue_comments, "review": review_comments}
    print(f"▶ Fetched diff ({len(pr_diff):,} chars), "
          f"{len(issue_comments)} issue + {len(review_comments)} review comments")

    # ---- Run dir + worktree ----
    run_dir = _new_run_dir(settings.runs_dir, pr)
    worktree_dir = run_dir / "checkout"
    print(f"▶ Run dir: {run_dir}")

    meta_path = run_dir / "meta.json"
    tool_log_path = run_dir / "tool_log.jsonl"
    transcript_path = run_dir / "transcript.md"
    review_path = run_dir / "review.md"

    # Persist initial meta so we can debug even if everything below crashes
    meta_path.write_text(json.dumps({
        "pr_url": pr.html_url,
        "owner": pr.owner, "repo": pr.repo, "number": pr.number,
        "head_sha": head_sha, "base_branch": base_branch,
        "model": settings.llm.model,
        "fallback_model": settings.llm.fallback_model,
        "effort": settings.llm.effort,
        "started_at": dt.datetime.now().isoformat(),
        "rails": {
            "max_tool_rounds": settings.loop.max_tool_rounds,
            "max_files_read": settings.loop.max_files_read,
            "max_chars_per_file": settings.loop.max_chars_per_file,
            "max_input_tokens": settings.loop.max_input_tokens,
            "total_timeout_seconds": settings.loop.total_timeout_seconds,
        },
    }, indent=2))

    # Use the worktree for the duration of the review; it's cleaned up on exit.
    review_markdown: str | None = None
    rounds = 0
    total_input = 0
    total_output = 0
    abort_reason: str | None = None
    cancelled = False
    text_content = ""
    files_inspected: list[str] = []

    try:
        with per_run_worktree(
            bare=bare, worktree_dir=worktree_dir, head_sha=head_sha,
        ) as repo_root:
            # ---- Tool context ----
            ctx = ToolContext(
                repo_root=repo_root,
                pr_number=pr.number, pr_owner=pr.owner, pr_repo=pr.repo,
                pr_diff=pr_diff, pr_metadata=pr_meta, pr_comments=pr_comments,
                rails=settings.loop,
            )

            # ---- Initial messages ----
            messages: list[dict] = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": render_user_prompt(
                    pr_metadata=pr_meta, pr_diff=pr_diff,
                )},
            ]
            tools_specs = get_tool_specs()

            transcript_lines: list[str] = [
                f"# Agentic Review Transcript",
                f"PR: {pr.html_url}",
                f"Model: {settings.llm.model} (effort={settings.llm.effort})",
                f"Started: {dt.datetime.now().isoformat()}",
                "",
                "## System prompt",
                f"<details><summary>(click)</summary>\n\n```\n{SYSTEM_PROMPT}\n```\n</details>",
                "",
                "## Initial user prompt",
                f"<details><summary>(click)</summary>\n\n```markdown\n{messages[1]['content'][:5000]}…\n```\n</details>",
                "",
                "## Tool loop",
            ]

            deadline = started + settings.loop.total_timeout_seconds

            # ---- The tool loop ----
            while True:
                # Cooperative cancellation point — asyncio.CancelledError can be
                # raised here when the registry cancels us due to a new push.
                await asyncio.sleep(0)

                rounds += 1
                if rounds > settings.loop.max_tool_rounds:
                    abort_reason = f"max_tool_rounds ({settings.loop.max_tool_rounds}) exceeded"
                    break
                if time.time() > deadline:
                    abort_reason = f"timeout ({settings.loop.total_timeout_seconds}s) exceeded"
                    break

                approx_input = _approx_message_tokens(messages)
                if approx_input > settings.loop.max_input_tokens:
                    abort_reason = (
                        f"approx input tokens {approx_input:,} > "
                        f"max_input_tokens {settings.loop.max_input_tokens:,}"
                    )
                    break

                print(f"  → round {rounds}/{settings.loop.max_tool_rounds} "
                      f"(approx {approx_input:,} input tokens)")

                try:
                    resp = await acompletion_with_tools(
                        model=settings.llm.model,
                        messages=messages,
                        tools=tools_specs,
                        vertex_project=settings.llm.vertex_project,
                        vertex_location=settings.llm.vertex_location,
                        effort=settings.llm.effort,
                        max_tokens=settings.llm.max_output_tokens,
                        timeout=120,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    tb = traceback.format_exc()
                    log.error("LLM call failed in round %d: %s", rounds, e)
                    transcript_lines.append(
                        f"\n### Round {rounds}: LLM ERROR\n```\n{e}\n\n{tb}\n```"
                    )
                    if rounds == 1 and settings.llm.fallback_model and settings.llm.fallback_model != settings.llm.model:
                        print(f"  ⚠ Trying fallback {settings.llm.fallback_model}")
                        try:
                            resp = await acompletion_with_tools(
                                model=settings.llm.fallback_model,
                                messages=messages, tools=tools_specs,
                                vertex_project=settings.llm.vertex_project,
                                vertex_location=settings.llm.vertex_location,
                                effort=settings.llm.effort,
                                max_tokens=settings.llm.max_output_tokens,
                                timeout=120,
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as e2:
                            abort_reason = f"both primary + fallback failed: {e2}"
                            break
                    else:
                        abort_reason = f"LLM error: {e}"
                        break

                # Track usage
                usage = getattr(resp, "usage", None)
                if usage:
                    total_input += getattr(usage, "prompt_tokens", 0) or 0
                    total_output += getattr(usage, "completion_tokens", 0) or 0

                choice = resp["choices"][0]
                msg = choice["message"]
                finish_reason = choice.get("finish_reason")
                if hasattr(msg, "model_dump"):
                    assistant_msg = msg.model_dump(exclude_none=True)
                elif hasattr(msg, "to_dict"):
                    assistant_msg = msg.to_dict()
                else:
                    assistant_msg = dict(msg)

                if assistant_msg.get("content") in (None, "") and assistant_msg.get("tool_calls"):
                    assistant_msg["content"] = ""

                tool_calls = assistant_msg.get("tool_calls") or []
                text_content = assistant_msg.get("content") or ""

                transcript_lines.append(
                    f"\n### Round {rounds} — finish_reason={finish_reason}, "
                    f"tool_calls={len(tool_calls)}, content_chars={len(text_content)}"
                )
                if text_content.strip():
                    transcript_lines.append(
                        f"\n**Assistant text:**\n```\n{text_content[:3000]}\n```"
                    )

                messages.append(assistant_msg)

                if not tool_calls:
                    if "submit_review" in text_content.lower():
                        review_markdown = text_content
                        abort_reason = "model returned text without calling submit_review (heuristic capture)"
                    else:
                        abort_reason = "model returned no tool calls and no submit_review"
                    break

                submit_called = False
                for tc in tool_calls:
                    fn = tc.get("function", {}) or {}
                    name = fn.get("name")
                    # Bug #8: synthesise an id if missing — Anthropic via litellm
                    # SHOULD always set one, but if it doesn't we'd 400 with
                    # tool_call_id=None.
                    tcall_id = tc.get("id") or f"synth_{uuid.uuid4().hex[:12]}"
                    raw_args = fn.get("arguments") or "{}"
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except Exception as e:
                        args = {}
                        tool_text = f"ERROR: bad JSON arguments: {e}"
                        _append_jsonl(tool_log_path, {
                            "round": rounds, "tool": name, "args_raw": raw_args,
                            "error": "bad_json", "result_chars": len(tool_text),
                        })
                    else:
                        if name == "submit_review":
                            review_markdown = (args.get("review_markdown") or "").strip()
                            tool_text = "Review submitted. Loop ending."
                            _append_jsonl(tool_log_path, {
                                "round": rounds, "tool": name,
                                "review_chars": len(review_markdown or ""),
                            })
                            submit_called = True
                        else:
                            handler = TOOL_HANDLERS.get(name)
                            if not handler:
                                tool_text = f"ERROR: unknown tool '{name}'"
                                _append_jsonl(tool_log_path, {
                                    "round": rounds, "tool": name, "error": "unknown_tool",
                                })
                            else:
                                try:
                                    result = handler(args, ctx)
                                    tool_text = result.display
                                    _append_jsonl(tool_log_path, {
                                        "round": rounds, "tool": name, "args": args,
                                        "result_chars": len(tool_text),
                                        "audit": result.audit,
                                        "truncated": result.truncated,
                                        "error": result.error,
                                    })
                                except Exception as e:
                                    tb = traceback.format_exc()
                                    tool_text = f"ERROR: tool '{name}' raised: {e}"
                                    _append_jsonl(tool_log_path, {
                                        "round": rounds, "tool": name, "args": args,
                                        "error": str(e), "traceback": tb,
                                    })

                    # Bug #3: hard cap on tool-result content size BEFORE appending
                    # to the conversation. The per-tool _truncate inside tools.py
                    # caps individual files, but read_files batch can stack up.
                    if len(tool_text) > MAX_TOOL_RESULT_CHARS:
                        tool_text = (
                            tool_text[:MAX_TOOL_RESULT_CHARS]
                            + f"\n\n[...tool result truncated to {MAX_TOOL_RESULT_CHARS:,} "
                              f"chars; original was {len(tool_text):,}. Make narrower "
                              f"queries or read_file with start_line/end_line ranges.]"
                        )

                    transcript_lines.append(
                        f"\n**Tool call:** `{name}({json.dumps(args, default=str)[:200]})` "
                        f"→ {len(tool_text):,} chars"
                    )
                    transcript_lines.append(
                        f"<details><summary>(result)</summary>\n\n```\n{tool_text[:3000]}\n```\n</details>"
                    )

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tcall_id,
                        "content": tool_text,
                    })

                    # Bug #2: when submit_review is in the same round as other
                    # tools, we MUST NOT execute the rest. Anthropic is normally
                    # disciplined about this but we belt-and-braces it.
                    if submit_called:
                        # Mark the remaining tool_calls as "skipped" so the model
                        # gets a complete tool_call_id->result mapping (some
                        # providers 400 if any tool_call lacks a response).
                        for skipped in tool_calls[tool_calls.index(tc) + 1 :]:
                            sk_id = skipped.get("id") or f"synth_{uuid.uuid4().hex[:12]}"
                            sk_name = (skipped.get("function") or {}).get("name", "?")
                            _append_jsonl(tool_log_path, {
                                "round": rounds, "tool": sk_name,
                                "skipped": "submit_review_in_same_round",
                            })
                            messages.append({
                                "role": "tool",
                                "tool_call_id": sk_id,
                                "content": f"SKIPPED: submit_review was called in this same round; this {sk_name} call was not executed.",
                            })
                        break

                if submit_called:
                    break

            files_inspected = sorted(ctx.files_read)

    except asyncio.CancelledError:
        cancelled = True
        abort_reason = "cancelled (likely a newer review started for this PR)"
        log.info("[reviewer] %s: cancelled mid-flight at round %d", pr.full_name, rounds)
        # Re-raise so callers (registry) see cancellation; finally block below still runs
        raise

    finally:
        duration = time.time() - started

        # ---- Persist final artifacts (ALWAYS — including on cancellation/exception) ----
        try:
            transcript_path.write_text("\n".join(transcript_lines))
        except (NameError, Exception) as e:
            log.warning("Could not write transcript: %s", e)

        try:
            if review_markdown:
                review_path.write_text(review_markdown)
            elif abort_reason or cancelled:
                review_path.write_text(
                    f"# Review NOT completed\n\n"
                    f"**Abort reason:** {abort_reason or 'unknown'}\n\n"
                    f"**Cancelled:** {cancelled}\n\n"
                    f"Last assistant text (if any):\n\n```\n{(text_content or '')[:5000]}\n```\n"
                )
        except Exception as e:
            log.warning("Could not write review.md: %s", e)

        try:
            meta = json.loads(meta_path.read_text())
            meta.update({
                "ended_at": dt.datetime.now().isoformat(),
                "duration_seconds": round(duration, 2),
                "rounds": rounds,
                "abort_reason": abort_reason,
                "cancelled": cancelled,
                "files_inspected": files_inspected,
                "files_inspected_count": len(files_inspected),
                "total_input_tokens": total_input,
                "total_output_tokens": total_output,
                "cost_estimate_usd": _estimate_cost(total_input, total_output, settings.llm.model),
                "review_chars": len(review_markdown or ""),
                "review_path": str(review_path),
            })
            meta_path.write_text(json.dumps(meta, indent=2, default=str))
        except Exception as e:
            log.warning("Could not update meta.json: %s", e)

        # Friendly stdout summary
        status = "🛑 CANCELLED" if cancelled else ("⚠ ABORTED" if abort_reason else "✅ Done")
        print(f"\n{status} in {duration:.1f}s — {rounds} rounds, "
              f"{len(files_inspected)} files inspected")
        if total_input or total_output:
            cost = _estimate_cost(total_input, total_output, settings.llm.model)
            print(f"   Tokens: {total_input:,} in / {total_output:,} out"
                  + (f" (~${cost:.2f})" if cost else ""))
        print(f"📄 Review: {review_path}")
        if abort_reason:
            print(f"⚠ Abort reason: {abort_reason}")

    return ReviewResult(
        success=bool(review_markdown),
        review_markdown=review_markdown or "",
        error=abort_reason,
        rounds=rounds,
        files_inspected=files_inspected,
        total_input_tokens=total_input,
        total_output_tokens=total_output,
        cost_estimate_usd=_estimate_cost(total_input, total_output, settings.llm.model),
        duration_seconds=duration,
        run_dir=run_dir,
        cancelled=cancelled,
    )


# --------------------------------------------------------------------------- #
# Convenience for production webhook integration (not used by CLI yet)
# --------------------------------------------------------------------------- #

async def review_pr_via_registry(pr_url: str, *, settings: Settings | None = None) -> None:
    """Wrap a review in the in-flight registry. New push → cancels prior run."""
    settings = settings or load_settings()
    pr = parse_pr_url(pr_url)

    # We need the head SHA for the registry; do a cheap fetch first.
    private_key = load_github_app_private_key()
    bundle = get_token_bundle(
        app_id=settings.github.app_id, private_key=private_key,
        owner=pr.owner, repo=pr.repo, base_url=settings.github.base_url,
    )
    pr_meta = fetch_pr(bundle.token, pr)
    head_sha = (pr_meta.get("head") or {}).get("sha", "")

    async def _runner() -> None:
        await _do_review(pr, settings)

    await REGISTRY.start_or_replace(
        pr=pr, head_sha=head_sha, runner=_runner,
        replace_reason="new push trigger",
    )


# --------------------------------------------------------------------------- #
# CLI entry
# --------------------------------------------------------------------------- #

def _setup_logging(verbosity: int = 1) -> None:
    level = logging.WARNING if verbosity == 0 else logging.INFO if verbosity == 1 else logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("LiteLLM").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.INFO)


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="Run agentic PR review locally")
    p.add_argument("pr_url", help="PR URL or owner/repo#N")
    p.add_argument("-v", "--verbose", action="count", default=1)
    args = p.parse_args()

    _setup_logging(args.verbose)

    try:
        result = asyncio.run(review_pr(args.pr_url))
        return 0 if result.success else 1
    except KeyboardInterrupt:
        print("\nInterrupted")
        return 130
    except Exception as e:
        log.exception("Top-level error: %s", e)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
