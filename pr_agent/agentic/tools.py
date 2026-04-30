"""Tools the model can call during the review loop.

Each tool returns a `ToolResult` that bundles a renderable string for the
model + a structured payload for our audit log.

Tools are *stateless* but receive a `ToolContext` carrying the open
workspace + bookkeeping for the per-run rails (files-read counter, etc).
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import LoopConfig
from .security import is_sensitive_path
from .workspace import safe_join


# --------------------------------------------------------------------------- #
# Context + result types
# --------------------------------------------------------------------------- #

@dataclass
class ToolContext:
    repo_root: Path
    pr_number: int
    pr_owner: str
    pr_repo: str
    pr_diff: str
    pr_metadata: dict
    pr_comments: dict   # {issue: [...], review: [...]}
    rails: LoopConfig
    files_read: set[str] = field(default_factory=set)  # tracking distinct files


@dataclass
class ToolResult:
    """Output of a tool call.

    `display` is what the model sees. `audit` is the rich payload we log.
    """
    display: str
    audit: dict[str, Any] = field(default_factory=dict)
    truncated: bool = False
    error: str | None = None


# --------------------------------------------------------------------------- #
# Tool implementations
# --------------------------------------------------------------------------- #

# These constants keep the model from spending too much budget on garbage paths
_BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".pdf",
    ".zip", ".gz", ".tar", ".bz2", ".xz", ".7z", ".rar",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp3", ".mp4", ".mov", ".webm", ".wav",
    ".class", ".jar", ".so", ".dylib", ".dll", ".exe",
    ".pyc", ".o", ".a", ".node", ".wasm",
}
# Bug #11: expanded skip list — these directories almost never contain
# review-relevant code, but commonly drown out search results.
_ALWAYS_SKIP_DIRS = {
    # VCS / IDE
    ".git", ".idea", ".vscode", ".vs",
    # JS/TS build/cache
    "node_modules", ".next", ".nuxt", ".cache", "dist", "build", "out",
    ".turbo", ".vercel", ".parcel-cache", ".rpt2_cache", ".angular",
    # Python build/cache
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".tox", ".eggs", "venv", ".venv", "env", "site-packages",
    # generated / coverage
    "coverage", ".coverage", "htmlcov", "__generated__",
    # infra/state
    ".terraform", ".terragrunt-cache", "tfstate", ".serverless",
    # mobile builds
    "Pods", "DerivedData", ".gradle",
    # tooling
    ".husky", ".github_archive",
}

# Bug #11: also skip these file globs in search/list (lockfiles, minified,
# generated, source maps).
_SEARCH_GLOB_EXCLUDES = (
    "*.lock", "*-lock.json", "package-lock.json", "yarn.lock",
    "pnpm-lock.yaml", "Cargo.lock", "Pipfile.lock", "poetry.lock",
    "composer.lock", "go.sum",
    "*.min.js", "*.min.css", "*.bundle.js",
    "*.map",   # source maps
    "*.generated.*", "*_generated.*", "*.gen.*", "*.pb.go", "*.pb.ts",
    "*.snap",  # jest snapshots
)


def _is_binary_path(p: Path) -> bool:
    return p.suffix.lower() in _BINARY_EXTS


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    head = text[: max_chars]
    tail_note = (
        f"\n\n[...truncated: showed first {max_chars} chars of {len(text)} total. "
        f"Use read_file with start_line/end_line to read further sections.]"
    )
    return head + tail_note, True


def tool_read_file(args: dict, ctx: ToolContext) -> ToolResult:
    path = args.get("path", "").strip()
    start_line = int(args.get("start_line", 1) or 1)
    end_line_arg = args.get("end_line")
    end_line = int(end_line_arg) if end_line_arg is not None else None

    try:
        full = safe_join(ctx.repo_root, path)
    except ValueError as e:
        return ToolResult(display=f"ERROR: {e}", error=str(e))

    if not full.exists():
        return ToolResult(display=f"ERROR: file not found: {path}", error="not_found")
    if full.is_dir():
        return ToolResult(
            display=f"ERROR: {path} is a directory, use list_directory()",
            error="is_directory",
        )
    if _is_binary_path(full):
        return ToolResult(
            display=f"ERROR: refusing to read binary file: {path}",
            error="binary",
        )

    # Sensitive-path deny-list (env files, secrets, private keys, etc.)
    if is_sensitive_path(path):
        return ToolResult(
            display=(
                f"ERROR: refusing to read sensitive file: {path}\n"
                f"(matches HEXIS deny-list — env files, secrets, private keys. "
                f"This is intentional safety, not a bug. Pick a different file.)"
            ),
            error="sensitive_path",
        )

    # Cost rail: max distinct files read
    if (
        path not in ctx.files_read
        and len(ctx.files_read) >= ctx.rails.max_files_read
    ):
        return ToolResult(
            display=(
                f"ERROR: max_files_read limit reached ({ctx.rails.max_files_read}). "
                f"You've already read: {sorted(ctx.files_read)[-10:]}... "
                f"Reason: focus your remaining attention on synthesising the review."
            ),
            error="files_quota_exhausted",
        )

    try:
        text = full.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return ToolResult(display=f"ERROR: could not read {path}: {e}", error=str(e))

    ctx.files_read.add(path)

    lines = text.splitlines()
    total_lines = len(lines)

    s = max(1, start_line)
    e = min(total_lines, end_line) if end_line else total_lines
    if s > total_lines:
        return ToolResult(
            display=f"ERROR: start_line {s} > file length {total_lines}",
            error="bad_range",
        )
    selected = lines[s - 1 : e]
    snippet = "\n".join(f"{s + i:>5}  {ln}" for i, ln in enumerate(selected))
    snippet, truncated = _truncate(snippet, ctx.rails.max_chars_per_file)

    header = (
        f"### {path} (lines {s}-{e} of {total_lines})\n"
    )
    return ToolResult(
        display=header + snippet,
        audit={
            "path": path, "start_line": s, "end_line": e,
            "total_lines": total_lines, "chars": len(snippet), "truncated": truncated,
        },
        truncated=truncated,
    )


def tool_read_files(args: dict, ctx: ToolContext) -> ToolResult:
    """Batch read N files in a single call. Returns each concatenated."""
    paths = args.get("paths") or []
    if not isinstance(paths, list) or not paths:
        return ToolResult(display="ERROR: 'paths' must be a non-empty list", error="bad_args")

    start_line = args.get("start_line", 1)
    end_line = args.get("end_line")

    chunks = []
    audit_chunks = []
    truncated_any = False
    for p in paths[:20]:  # hard cap per call
        sub = tool_read_file(
            {"path": p, "start_line": start_line, "end_line": end_line}, ctx,
        )
        chunks.append(sub.display)
        audit_chunks.append({"path": p, **sub.audit})
        if sub.truncated:
            truncated_any = True
    return ToolResult(
        display="\n\n---\n\n".join(chunks),
        audit={"files": audit_chunks},
        truncated=truncated_any,
    )


def tool_list_directory(args: dict, ctx: ToolContext) -> ToolResult:
    path = (args.get("path") or ".").strip()
    try:
        full = safe_join(ctx.repo_root, path) if path != "." else ctx.repo_root
    except ValueError as e:
        return ToolResult(display=f"ERROR: {e}", error=str(e))

    if not full.exists():
        return ToolResult(display=f"ERROR: directory not found: {path}", error="not_found")
    if not full.is_dir():
        return ToolResult(display=f"ERROR: not a directory: {path}", error="not_dir")

    entries = []
    for item in sorted(full.iterdir()):
        if item.name in _ALWAYS_SKIP_DIRS:
            continue
        kind = "dir" if item.is_dir() else "file"
        try:
            size = item.stat().st_size if item.is_file() else None
        except OSError:
            size = None
        size_str = f" ({size:,} bytes)" if size is not None else ""
        entries.append(f"  {kind:4s}  {item.name}{size_str}")

    if not entries:
        return ToolResult(display=f"### {path}/\n(empty)", audit={"path": path, "count": 0})

    return ToolResult(
        display=f"### {path}/ ({len(entries)} entries)\n" + "\n".join(entries),
        audit={"path": path, "count": len(entries)},
    )


def tool_search_codebase(args: dict, ctx: ToolContext) -> ToolResult:
    """ripgrep search. Returns top-N matches with N lines of context."""
    query = (args.get("query") or "").strip()
    if not query:
        return ToolResult(display="ERROR: 'query' required", error="bad_args")

    file_glob = args.get("file_glob")
    max_results = int(args.get("max_results") or ctx.rails.search_max_results)
    context_lines = int(args.get("context_lines") or ctx.rails.search_context_lines)

    cmd = [
        "rg", "--no-config", "--color=never",
        "--max-count=5",          # per-file: enough for context, not flooding
        f"-C{context_lines}",     # before/after
        "--max-filesize=500K",
        "--hidden",
        "--glob=!.git",
    ]
    for d in _ALWAYS_SKIP_DIRS:
        cmd.append(f"--glob=!**/{d}/**")
    for g in _SEARCH_GLOB_EXCLUDES:
        cmd.append(f"--glob=!{g}")
    if file_glob:
        cmd.append(f"--glob={file_glob}")
    cmd += ["--", query, "."]

    try:
        proc = subprocess.run(
            cmd, cwd=ctx.repo_root, capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(display="ERROR: search timed out (10s)", error="timeout")

    if proc.returncode == 1 and not proc.stdout:
        return ToolResult(
            display=f"### search '{query}' — no matches",
            audit={"query": query, "matches": 0},
        )
    if proc.returncode > 1:
        return ToolResult(
            display=f"ERROR: rg failed: {proc.stderr.strip()}",
            error=proc.stderr.strip(),
        )

    out = proc.stdout
    # Truncate to roughly max_results match-blocks
    blocks = out.split("\n--\n")
    truncated = False
    if len(blocks) > max_results:
        blocks = blocks[:max_results]
        truncated = True
    rendered = "\n--\n".join(blocks)
    rendered, char_truncated = _truncate(rendered, 40000)

    note = (f" (truncated to {max_results} blocks)" if truncated else "") + (
        f" (output truncated at 40K chars)" if char_truncated else "")
    return ToolResult(
        display=f"### search '{query}'{note}\n```\n{rendered}\n```",
        audit={
            "query": query, "file_glob": file_glob,
            "blocks": min(len(blocks), max_results), "truncated": truncated,
        },
        truncated=truncated or char_truncated,
    )


def tool_find_definition(args: dict, ctx: ToolContext) -> ToolResult:
    """Heuristic definition finder via ripgrep. Works for TS/JS/Py/Go/Java."""
    symbol = (args.get("symbol") or "").strip()
    if not symbol:
        return ToolResult(display="ERROR: 'symbol' required", error="bad_args")
    safe_sym = re.escape(symbol)
    # patterns covering most languages
    patterns = [
        rf"\b(function|class|interface|type|enum)\s+{safe_sym}\b",
        rf"\b(const|let|var)\s+{safe_sym}\s*[:=]",
        rf"\bdef\s+{safe_sym}\s*\(",
        rf"\bclass\s+{safe_sym}\s*[\(:]",
        rf"\bexport\s+(default\s+)?(function|class|interface|type|enum|const|let)\s+{safe_sym}\b",
        rf"\bexport\s+\{{[^}}]*\b{safe_sym}\b[^}}]*\}}",
        rf"^\s*{safe_sym}\s*[:=]\s*(\(|\{{|function|async)",
        rf"\bfunc\s+\(?[^)]*\)?\s*{safe_sym}\s*\(",  # Go
        rf"\bpublic\s+[\w<>\[\]]+\s+{safe_sym}\s*\(",  # Java/C#
    ]
    big_re = "|".join(patterns)

    cmd = [
        "rg", "--no-config", "--color=never", "-n", "--no-heading",
        "--max-count=10", "--max-filesize=500K", "--hidden",
        "--glob=!.git",
    ]
    for d in _ALWAYS_SKIP_DIRS:
        cmd.append(f"--glob=!**/{d}/**")
    for g in _SEARCH_GLOB_EXCLUDES:
        cmd.append(f"--glob=!{g}")
    cmd += ["-e", big_re, "."]

    try:
        proc = subprocess.run(
            cmd, cwd=ctx.repo_root, capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(display="ERROR: find_definition timed out", error="timeout")

    if proc.returncode == 1 and not proc.stdout:
        return ToolResult(
            display=f"### find_definition '{symbol}' — no matches",
            audit={"symbol": symbol, "matches": 0},
        )
    if proc.returncode > 1:
        return ToolResult(
            display=f"ERROR: rg failed: {proc.stderr.strip()}", error=proc.stderr.strip(),
        )

    lines = proc.stdout.strip().splitlines()[:30]
    rendered = "\n".join(lines)
    return ToolResult(
        display=f"### find_definition '{symbol}'\n```\n{rendered}\n```",
        audit={"symbol": symbol, "matches": len(lines)},
    )


def tool_find_callers(args: dict, ctx: ToolContext) -> ToolResult:
    """Find places that call/use a symbol, excluding the definition itself.

    Heuristic: search for `symbol(` and `symbol.` and `.symbol(` and `from … {symbol}`.
    """
    symbol = (args.get("symbol") or "").strip()
    if not symbol:
        return ToolResult(display="ERROR: 'symbol' required", error="bad_args")
    safe_sym = re.escape(symbol)
    pattern = (
        rf"(\b{safe_sym}\s*\(|"        # foo(
        rf"\b\.{safe_sym}\b|"          # .foo
        rf"\bfrom\s+['\"][^'\"]+['\"]\s*;?\s*//.*\b{safe_sym}\b|"  # rare
        rf"\{{[^}}]*\b{safe_sym}\b[^}}]*\}}\s+from)"  # { foo } from
    )

    cmd = [
        "rg", "--no-config", "--color=never", "-n", "--no-heading",
        "--max-count=5",       # per-file
        "--max-filesize=500K",
        "--hidden", "--glob=!.git",
    ]
    for d in _ALWAYS_SKIP_DIRS:
        cmd.append(f"--glob=!**/{d}/**")
    for g in _SEARCH_GLOB_EXCLUDES:
        cmd.append(f"--glob=!{g}")
    cmd += ["-e", pattern, "."]

    try:
        proc = subprocess.run(
            cmd, cwd=ctx.repo_root, capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(display="ERROR: find_callers timed out", error="timeout")

    if proc.returncode == 1 and not proc.stdout:
        return ToolResult(
            display=f"### find_callers '{symbol}' — no matches",
            audit={"symbol": symbol, "matches": 0},
        )
    if proc.returncode > 1:
        return ToolResult(
            display=f"ERROR: rg failed: {proc.stderr.strip()}", error=proc.stderr.strip(),
        )

    lines = proc.stdout.strip().splitlines()[:50]
    rendered = "\n".join(lines)
    return ToolResult(
        display=f"### find_callers '{symbol}' ({len(lines)} hits, top 50)\n```\n{rendered}\n```",
        audit={"symbol": symbol, "matches": len(lines)},
    )


def tool_get_pr_diff(args: dict, ctx: ToolContext) -> ToolResult:
    diff = ctx.pr_diff or ""
    truncated = False
    if len(diff) > 200_000:
        diff = diff[:200_000] + "\n\n[...diff truncated]"
        truncated = True
    return ToolResult(
        display=f"### PR diff ({len(ctx.pr_diff):,} chars)\n```diff\n{diff}\n```",
        audit={"diff_chars": len(ctx.pr_diff), "truncated": truncated},
        truncated=truncated,
    )


def tool_get_pr_metadata(args: dict, ctx: ToolContext) -> ToolResult:
    md = ctx.pr_metadata or {}
    summary = {
        "title": md.get("title"),
        "number": md.get("number"),
        "html_url": md.get("html_url"),
        "state": md.get("state"),
        "draft": md.get("draft"),
        "author": (md.get("user") or {}).get("login"),
        "created_at": md.get("created_at"),
        "updated_at": md.get("updated_at"),
        "head_branch": (md.get("head") or {}).get("ref"),
        "head_sha": (md.get("head") or {}).get("sha"),
        "base_branch": (md.get("base") or {}).get("ref"),
        "additions": md.get("additions"),
        "deletions": md.get("deletions"),
        "changed_files": md.get("changed_files"),
        "commits": md.get("commits"),
        "labels": [l.get("name") for l in (md.get("labels") or [])],
        "description_markdown": md.get("body") or "",
    }
    return ToolResult(
        display="### PR metadata\n```json\n" + json.dumps(summary, indent=2) + "\n```",
        audit=summary,
    )


def tool_get_pr_comments(args: dict, ctx: ToolContext) -> ToolResult:
    issue = ctx.pr_comments.get("issue", []) or []
    review = ctx.pr_comments.get("review", []) or []
    parts = ["### Top-level PR comments"]
    if not issue:
        parts.append("(none)")
    for c in issue:
        user = (c.get("user") or {}).get("login", "?")
        when = c.get("created_at", "")
        body = (c.get("body") or "").strip()
        if body:
            parts.append(f"\n**@{user}** ({when}):\n{body}")
    parts.append("\n---\n### Inline review comments")
    if not review:
        parts.append("(none)")
    # group by thread root for readability
    by_root: dict[int, list[dict]] = {}
    for c in review:
        root = c.get("in_reply_to_id") or c.get("id")
        by_root.setdefault(root, []).append(c)
    for root_id, thread in by_root.items():
        thread.sort(key=lambda x: x.get("created_at", ""))
        head = thread[0]
        path = head.get("path", "?")
        line = head.get("line") or head.get("original_line") or "?"
        parts.append(f"\n**Thread on `{path}:{line}`**")
        for c in thread:
            user = (c.get("user") or {}).get("login", "?")
            body = (c.get("body") or "").strip()
            if body:
                parts.append(f"  - @{user}: {body}")
    text = "\n".join(parts)
    text, truncated = _truncate(text, 60000)
    return ToolResult(
        display=text,
        audit={"issue_count": len(issue), "review_count": len(review)},
        truncated=truncated,
    )


def tool_git_log(args: dict, ctx: ToolContext) -> ToolResult:
    """Recent commits — optionally limited to a path."""
    n = int(args.get("n", 10))
    path = args.get("path")
    cmd = [
        "git", "log", f"-{n}",
        "--pretty=format:%h | %an | %ad | %s", "--date=short",
    ]
    if path:
        try:
            safe_join(ctx.repo_root, path)
        except ValueError as e:
            return ToolResult(display=f"ERROR: {e}", error=str(e))
        cmd += ["--", path]
    try:
        proc = subprocess.run(cmd, cwd=ctx.repo_root, capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired:
        return ToolResult(display="ERROR: git log timed out", error="timeout")
    if proc.returncode != 0:
        return ToolResult(display=f"ERROR: git log failed: {proc.stderr.strip()}", error=proc.stderr.strip())
    return ToolResult(
        display=f"### git log -{n}{(' ' + path) if path else ''}\n```\n{proc.stdout.strip()}\n```",
        audit={"n": n, "path": path},
    )


# --------------------------------------------------------------------------- #
# Tool registry: maps name -> (handler, JSON-schema dict)
# --------------------------------------------------------------------------- #

ToolHandler = Callable[[dict, ToolContext], ToolResult]


def get_tool_specs() -> list[dict]:
    """OpenAI-style tool schemas (LiteLLM forwards these to Anthropic)."""
    return [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": (
                    "Read a single file from the repo. Use start_line/end_line "
                    "to read a section of a large file. Returns content with line numbers. "
                    "Files are read by path relative to repo root, e.g. 'src/foo.ts'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Path relative to repo root"},
                        "start_line": {"type": "integer", "description": "1-based, default 1", "default": 1},
                        "end_line": {"type": "integer", "description": "Inclusive, default end-of-file"},
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_files",
                "description": (
                    "Batch-read multiple files in a single tool call (more efficient). "
                    "Same start_line/end_line apply to ALL paths in the batch. "
                    "Use this when you know which 3-10 files you want at once."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "1-20 paths relative to repo root",
                        },
                        "start_line": {"type": "integer", "default": 1},
                        "end_line": {"type": "integer"},
                    },
                    "required": ["paths"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_directory",
                "description": (
                    "List entries in a directory. Use '.' for repo root. "
                    "Hides .git, node_modules, dist, build, .next, __pycache__, etc."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "default": ".", "description": "Path relative to repo root"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_codebase",
                "description": (
                    "ripgrep across the repo. Returns matches with surrounding lines. "
                    "Use this AGGRESSIVELY before reading whole files — search results "
                    "with context are usually enough. Optional file_glob like '*.ts'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Pattern (regex)"},
                        "file_glob": {"type": "string", "description": "e.g. '*.ts', '!*.test.ts'"},
                        "max_results": {"type": "integer", "default": 50},
                        "context_lines": {"type": "integer", "default": 3},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "find_definition",
                "description": (
                    "Find where a symbol is DEFINED (function/class/type/const). "
                    "Heuristic across TS/JS/Python/Go/Java patterns."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                    },
                    "required": ["symbol"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "find_callers",
                "description": (
                    "Find places that CALL or USE a symbol — useful for assessing "
                    "the blast radius of a change."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                    },
                    "required": ["symbol"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_pr_diff",
                "description": "Return the full PR diff. Already provided in the initial prompt; only call again if you need to re-examine.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_pr_metadata",
                "description": "PR title/branch/labels/description/etc as JSON.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_pr_comments",
                "description": (
                    "Fetch all existing PR conversation: top-level comments + "
                    "inline-thread comments grouped by file:line."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "git_log",
                "description": "Last N commits (optionally restricted to a path).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "n": {"type": "integer", "default": 10},
                        "path": {"type": "string"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "submit_review",
                "description": (
                    "FINAL STEP: submit your completed review as Markdown. "
                    "Calling this ENDS the review loop. "
                    "Do not call this until you have actually inspected the code."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "review_markdown": {
                            "type": "string",
                            "description": (
                                "The complete review in Markdown. Must include sections: "
                                "TL;DR, Estimated effort to review (1-5), Security concerns, "
                                "Key findings (severity-ranked), Files inspected, "
                                "Questions for the author."
                            ),
                        },
                    },
                    "required": ["review_markdown"],
                },
            },
        },
    ]


TOOL_HANDLERS: dict[str, ToolHandler] = {
    "read_file": tool_read_file,
    "read_files": tool_read_files,
    "list_directory": tool_list_directory,
    "search_codebase": tool_search_codebase,
    "find_definition": tool_find_definition,
    "find_callers": tool_find_callers,
    "get_pr_diff": tool_get_pr_diff,
    "get_pr_metadata": tool_get_pr_metadata,
    "get_pr_comments": tool_get_pr_comments,
    "git_log": tool_git_log,
    # 'submit_review' is special — handled by the loop driver, not here.
}
