"""Runtime configuration for the agentic reviewer.

All values can be overridden by env vars (see .env at the repo root).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


# ----- Paths -----
# Production override: set AGENTIC_DATA_DIR to point at a host-mounted volume
# (e.g. /app/agentic-data inside the container, mapped to host
# /root/hexis/pr-agent/agentic-data). When unset (local CLI use case from the
# `agentic-reviews/` workspace) we fall back to the package's parent dir.
HERE = Path(__file__).resolve().parent
_DEFAULT_WORKSPACE = HERE.parent     # for local CLI: agentic-reviews/
_OVERRIDE = os.getenv("AGENTIC_DATA_DIR")
WORKSPACE = Path(_OVERRIDE) if _OVERRIDE else _DEFAULT_WORKSPACE
REPOS_DIR = WORKSPACE / "repos"
RUNS_DIR = WORKSPACE / "runs"

# Where the production GitHub App credentials live (re-used so we don't
# need a separate key for the agentic reviewer).
# In production (inside docker) the .env / .secrets.toml are mounted at:
#   /app/.env  /app/pr_agent/settings/.secrets.toml
# Local CLI uses /root/hexis/pr-agent/.env + /root/hexis/pr-agent/.secrets.toml
PR_AGENT_ROOT = WORKSPACE.parent
ENV_FILE = Path(os.getenv("AGENTIC_ENV_FILE") or (PR_AGENT_ROOT / ".env"))
SECRETS_FILE = Path(
    os.getenv("AGENTIC_SECRETS_FILE")
    or (PR_AGENT_ROOT / ".secrets.toml")
)


# ----- LLM -----
@dataclass
class LLMConfig:
    """Defaults for the LiteLLM call. All env-overridable."""
    model: str = field(default_factory=lambda: os.getenv(
        "AGENTIC_MODEL", "vertex_ai/claude-opus-4-8"
    ))
    fallback_model: str = field(default_factory=lambda: os.getenv(
        "AGENTIC_FALLBACK_MODEL", "vertex_ai/claude-opus-4-7"
    ))
    vertex_project: str = field(default_factory=lambda: (
        os.getenv("VERTEXAI.VERTEX_PROJECT")
        or os.getenv("VERTEXAI__VERTEX_PROJECT")
        or os.getenv("VERTEX_PROJECT")
        or "hexis-dev-infrastructure"
    ))
    vertex_location: str = field(default_factory=lambda: (
        os.getenv("VERTEXAI.VERTEX_LOCATION")
        or os.getenv("VERTEXAI__VERTEX_LOCATION")
        or os.getenv("VERTEX_LOCATION")
        or "global"
    ))
    effort: str = field(default_factory=lambda: os.getenv(
        "AGENTIC_EFFORT", "max"
    ))  # one of: low, medium, high, xhigh, max
    # Opus 4.x family supports up to 128K output on Vertex AI per the latest
    # litellm model map; we cap at 64K to keep individual review costs bounded.
    max_output_tokens: int = field(default_factory=lambda: int(os.getenv(
        "AGENTIC_MAX_OUTPUT_TOKENS", "64000"
    )))


# ----- Loop / safety rails -----
@dataclass
class LoopConfig:
    """Caps the model can't exceed. Tunable from env."""
    # Bumped 40 -> 100 to give the agent enough rounds to handle large PRs
    # (e.g. cross-file refactors that need many search + read iterations).
    max_tool_rounds: int = field(default_factory=lambda: int(os.getenv(
        "AGENTIC_MAX_TOOL_ROUNDS", "100"
    )))
    max_files_read: int = field(default_factory=lambda: int(os.getenv(
        "AGENTIC_MAX_FILES_READ", "50"
    )))
    max_chars_per_file: int = field(default_factory=lambda: int(os.getenv(
        "AGENTIC_MAX_CHARS_PER_FILE", "80000"
    )))
    max_input_tokens: int = field(default_factory=lambda: int(os.getenv(
        "AGENTIC_MAX_INPUT_TOKENS", "800000"
    )))
    # Bumped 300s -> 900s (15min) for the same reason. Opus 4.8 + max-effort
    # thinking on a 50+ tool-round review can comfortably exceed 5 minutes.
    total_timeout_seconds: int = field(default_factory=lambda: int(os.getenv(
        "AGENTIC_TIMEOUT_SECONDS", "900"
    )))
    # search_codebase
    search_max_results: int = field(default_factory=lambda: int(os.getenv(
        "AGENTIC_SEARCH_MAX_RESULTS", "50"
    )))
    search_context_lines: int = field(default_factory=lambda: int(os.getenv(
        "AGENTIC_SEARCH_CONTEXT_LINES", "3"
    )))


@dataclass
class GitHubConfig:
    """GitHub App auth for cloning private repos."""
    # The .env file uses dot-notation (GITHUB.APP_ID); also accept common variants.
    app_id: str = field(default_factory=lambda: (
        os.getenv("GITHUB.APP_ID")
        or os.getenv("GITHUB__APP_ID")
        or os.getenv("GITHUB_APP_ID")
        or ""
    ))
    # private key: read from .secrets.toml at startup (not env)
    base_url: str = field(default_factory=lambda: (
        os.getenv("GITHUB.BASE_URL")
        or os.getenv("GITHUB__BASE_URL")
        or os.getenv("GITHUB_BASE_URL")
        or "https://api.github.com"
    ))


@dataclass
class Settings:
    llm: LLMConfig = field(default_factory=LLMConfig)
    loop: LoopConfig = field(default_factory=LoopConfig)
    github: GitHubConfig = field(default_factory=GitHubConfig)
    workspace: Path = WORKSPACE
    repos_dir: Path = REPOS_DIR
    runs_dir: Path = RUNS_DIR


def load_settings() -> Settings:
    """Load .env + .secrets.toml first, then build Settings."""
    # Load production .env so we share GITHUB__APP_ID, VERTEXAI__VERTEX_PROJECT, etc.
    if ENV_FILE.exists():
        from dotenv import load_dotenv
        load_dotenv(ENV_FILE, override=False)
    return Settings()


def load_github_app_private_key() -> str:
    """Pull the GitHub App private key out of .secrets.toml.

    In the production webhook server the private key is already loaded into
    PR-Agent's dynaconf settings, so try that first; fall back to reading
    .secrets.toml directly (used by the local CLI).
    """
    # Try dynaconf-loaded settings first
    try:
        from pr_agent.config_loader import get_settings
        pk = get_settings().get("GITHUB.PRIVATE_KEY")
        if pk:
            return pk
    except Exception:
        pass
    # Fallback: read .secrets.toml directly
    if not SECRETS_FILE.exists():
        raise FileNotFoundError(
            f"GitHub App private key not found in dynaconf settings, and "
            f"{SECRETS_FILE} doesn't exist"
        )
    import tomli
    with open(SECRETS_FILE, "rb") as fh:
        secrets = tomli.load(fh)
    pk = secrets.get("github", {}).get("private_key")
    if not pk:
        raise ValueError("github.private_key missing from .secrets.toml")
    return pk
