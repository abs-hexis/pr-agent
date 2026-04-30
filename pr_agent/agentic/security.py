"""Sensitive-path deny-list.

Refuse to read files that are likely to contain secrets, even though the
GitHub App probably wouldn't have injected them via PR review anyway.
This is defense-in-depth: don't surface a real .env file to the model.

Opt-out: set AGENTIC_ALLOW_SENSITIVE_FILES=true in the env.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path


# Glob patterns matched against the path RELATIVE to repo root (no leading "/")
# Matched case-insensitively.
SENSITIVE_GLOBS: tuple[str, ...] = (
    # env files
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    # secrets directories
    "**/secrets/**",
    "**/secret/**",
    "**/.secrets/**",
    # private keys
    "**/*.pem",
    "**/*.key",
    "**/id_rsa*",
    "**/id_ed25519*",
    "**/*.p12",
    "**/*.pfx",
    # auth state / credentials
    "**/credentials*",
    "**/credentials.json",
    "**/.npmrc",
    "**/.pypirc",
    "**/*.kubeconfig",
    # google cloud / service accounts
    "**/service-account*.json",
    "**/serviceaccount*.json",
    "**/gha-creds-*.json",
    # password files
    "**/htpasswd*",
    "**/.htpasswd*",
    # ssh
    "**/.ssh/**",
)


def is_sensitive_path(rel_path: str) -> bool:
    """True if the path matches a sensitive pattern.

    rel_path is the path RELATIVE to repo root (e.g. ".env" or "src/foo.ts").
    """
    if os.getenv("AGENTIC_ALLOW_SENSITIVE_FILES", "").lower() in ("1", "true", "yes"):
        return False
    p = rel_path.lstrip("/").lower()
    # fnmatch is case-sensitive; we lowercased above
    for pat in SENSITIVE_GLOBS:
        if fnmatch.fnmatch(p, pat.lower()):
            return True
        # Also try without the leading **/  for top-level matches
        if pat.startswith("**/") and fnmatch.fnmatch(p, pat[3:].lower()):
            return True
    return False
