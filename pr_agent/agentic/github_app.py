"""GitHub App authentication helpers.

We re-use the production PR-Agent GitHub App credentials so we don't need
to mint a separate set of secrets just for the local agentic reviewer.

Two-step auth:
  1. Mint a JWT signed with the app's private key (valid 9 minutes)
  2. Exchange JWT for an installation access token (valid 60 minutes)
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import jwt
import requests


# --------------------------------------------------------------------------- #
# Friendly exception types — Bug #5 fix
# --------------------------------------------------------------------------- #

class GitHubAPIError(RuntimeError):
    """Base class for friendly GitHub API errors."""
    def __init__(self, message: str, *, status_code: int | None = None,
                 url: str | None = None, response_text: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.url = url
        self.response_text = response_text


class PRNotAccessible(GitHubAPIError):
    """Bot can't access this PR (404 / 403)."""


class GitHubRateLimited(GitHubAPIError):
    """GitHub API rate-limited us (HTTP 429 or 403 with rate-limit headers)."""


def _raise_for_pr_status(r: requests.Response, *, pr: "PRRef") -> None:
    """Translate a non-2xx response into a friendly exception.

    Use INSTEAD of `r.raise_for_status()` whenever the URL is PR-scoped.
    """
    if 200 <= r.status_code < 300:
        return
    text = (r.text or "")[:300]
    common = {
        "status_code": r.status_code,
        "url": r.url,
        "response_text": text,
    }
    if r.status_code == 404:
        raise PRNotAccessible(
            f"PR {pr.full_name}#{pr.number} not accessible to this GitHub App "
            f"installation (HTTP 404). Either the PR doesn't exist or the App "
            f"isn't installed on this repo.",
            **common,
        )
    if r.status_code == 403:
        # Could be auth, suspended app, or rate limit
        if "rate limit" in text.lower() or r.headers.get("X-RateLimit-Remaining") == "0":
            reset = r.headers.get("X-RateLimit-Reset", "?")
            raise GitHubRateLimited(
                f"GitHub API rate-limited (resets at unix-ts={reset})",
                **common,
            )
        raise PRNotAccessible(
            f"PR {pr.full_name}#{pr.number} forbidden (HTTP 403). "
            f"Check the GitHub App's permissions on this repo. "
            f"Response: {text!r}",
            **common,
        )
    if r.status_code == 410:
        raise PRNotAccessible(
            f"PR {pr.full_name}#{pr.number} is gone (HTTP 410). "
            f"The repo may have been deleted.",
            **common,
        )
    if r.status_code == 429:
        raise GitHubRateLimited(
            f"GitHub API rate-limited (HTTP 429)",
            **common,
        )
    # Anything else: generic
    raise GitHubAPIError(
        f"GitHub API error: HTTP {r.status_code} on {r.url}\n  {text!r}",
        **common,
    )


@dataclass
class TokenBundle:
    """Cached installation token + when it was minted.

    Long-running reviews (and the eventually-wired webhook server) can hold
    a TokenBundle and call .refresh_if_stale() before any GitHub call to
    avoid 401s.
    """
    app_id: str
    private_key: str
    owner: str
    repo: str
    base_url: str
    installation_id: int
    token: str
    minted_at: float
    # Refresh after this many seconds (GitHub tokens are valid 1h, refresh at 50min)
    max_age_seconds: int = 50 * 60

    def is_stale(self) -> bool:
        return (time.time() - self.minted_at) > self.max_age_seconds

    def refresh_if_stale(self) -> "TokenBundle":
        if not self.is_stale():
            return self
        self.token = get_installation_token(
            app_id=self.app_id, private_key=self.private_key,
            installation_id=self.installation_id, base_url=self.base_url,
        )
        self.minted_at = time.time()
        return self

    @property
    def clone_url(self) -> str:
        """https URL with token embedded — pass to git clone / git fetch."""
        return f"https://x-access-token:{self.token}@github.com/{self.owner}/{self.repo}.git"


@dataclass
class PRRef:
    """Parsed reference to a GitHub pull request."""
    owner: str
    repo: str
    number: int

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def html_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}/pull/{self.number}"

    @property
    def api_url(self) -> str:
        return f"https://api.github.com/repos/{self.owner}/{self.repo}/pulls/{self.number}"


def parse_pr_url(url: str) -> PRRef:
    """Accept any of:
        https://github.com/abs-hexis/coach-desktop/pull/573
        https://github.com/abs-hexis/coach-desktop/pull/573/files
        abs-hexis/coach-desktop#573
        api.github.com/repos/abs-hexis/coach-desktop/pulls/573
    """
    s = url.strip()
    # short form like abs-hexis/coach-desktop#573
    if "#" in s and "://" not in s:
        repo_part, num = s.rsplit("#", 1)
        owner, repo = repo_part.strip("/").split("/", 1)
        return PRRef(owner=owner, repo=repo, number=int(num))
    # URL form
    p = urlparse(s)
    parts = [x for x in p.path.split("/") if x]
    # api: /repos/{owner}/{repo}/pulls/{n}/...
    if "repos" in parts and "pulls" in parts:
        i = parts.index("repos")
        owner, repo = parts[i + 1], parts[i + 2]
        n_idx = parts.index("pulls")
        return PRRef(owner=owner, repo=repo, number=int(parts[n_idx + 1]))
    # web: /{owner}/{repo}/pull/{n}/...
    if "pull" in parts:
        n_idx = parts.index("pull")
        owner, repo = parts[n_idx - 2], parts[n_idx - 1]
        return PRRef(owner=owner, repo=repo, number=int(parts[n_idx + 1]))
    raise ValueError(f"Could not parse PR url/ref: {url!r}")


# ----- Token minting -----

def _make_jwt(app_id: str, private_key: str) -> str:
    now = int(time.time())
    payload = {
        "iat": now - 60,           # tolerate small clock drift
        "exp": now + 9 * 60,       # GitHub max is 10 min
        "iss": app_id,
    }
    return jwt.encode(payload, private_key, algorithm="RS256")


def get_installation_id_for_repo(
    *, app_id: str, private_key: str, owner: str, repo: str,
    base_url: str = "https://api.github.com",
) -> int:
    """Find which App installation has access to {owner}/{repo}."""
    token = _make_jwt(app_id, private_key)
    r = requests.get(
        f"{base_url}/repos/{owner}/{repo}/installation",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=10,
    )
    r.raise_for_status()
    return int(r.json()["id"])


def get_installation_token(
    *, app_id: str, private_key: str, installation_id: int,
    base_url: str = "https://api.github.com",
) -> str:
    """Exchange JWT for a per-installation access token (valid 1h)."""
    token = _make_jwt(app_id, private_key)
    r = requests.post(
        f"{base_url}/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["token"]


# ----- Convenience: end-to-end clone URL -----

def get_authenticated_clone_url(
    *, app_id: str, private_key: str, owner: str, repo: str,
    base_url: str = "https://api.github.com",
) -> tuple[str, str]:
    """Return (clone_url, installation_token).

    clone_url embeds the token so a plain `git clone` works for private repos.

    NOTE: this is the legacy helper. New code should prefer `get_token_bundle`
    which returns a refreshable TokenBundle.
    """
    inst = get_installation_id_for_repo(
        app_id=app_id, private_key=private_key,
        owner=owner, repo=repo, base_url=base_url,
    )
    token = get_installation_token(
        app_id=app_id, private_key=private_key,
        installation_id=inst, base_url=base_url,
    )
    clone_url = f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"
    return clone_url, token


def get_token_bundle(
    *, app_id: str, private_key: str, owner: str, repo: str,
    base_url: str = "https://api.github.com",
) -> TokenBundle:
    """Mint a fresh installation token bundle. Use bundle.refresh_if_stale()
    before each long-running operation.
    """
    inst = get_installation_id_for_repo(
        app_id=app_id, private_key=private_key,
        owner=owner, repo=repo, base_url=base_url,
    )
    token = get_installation_token(
        app_id=app_id, private_key=private_key,
        installation_id=inst, base_url=base_url,
    )
    return TokenBundle(
        app_id=app_id, private_key=private_key,
        owner=owner, repo=repo, base_url=base_url,
        installation_id=inst, token=token, minted_at=time.time(),
    )


# ----- PR metadata fetch -----

def fetch_pr(token: str, pr: PRRef, *, base_url: str = "https://api.github.com") -> dict:
    r = requests.get(
        pr.api_url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=15,
    )
    _raise_for_pr_status(r, pr=pr)
    return r.json()


def fetch_pr_files(token: str, pr: PRRef, *, base_url: str = "https://api.github.com") -> list[dict]:
    """Returns list of {filename, status, additions, deletions, patch?}."""
    r = requests.get(
        f"{pr.api_url}/files?per_page=300",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=15,
    )
    _raise_for_pr_status(r, pr=pr)
    return r.json()


def fetch_pr_diff(token: str, pr: PRRef) -> str:
    r = requests.get(
        pr.api_url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3.diff",
        },
        timeout=30,
    )
    _raise_for_pr_status(r, pr=pr)
    return r.text


def fetch_pr_comments(token: str, pr: PRRef) -> tuple[list[dict], list[dict]]:
    """Returns (issue_comments, review_comments).

    issue_comments = top-level PR conversation tab
    review_comments = inline file-line comments and replies
    """
    h = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    issue_url = pr.api_url.replace("/pulls/", "/issues/") + "/comments"
    r1 = requests.get(issue_url + "?per_page=200", headers=h, timeout=15)
    _raise_for_pr_status(r1, pr=pr)
    r2 = requests.get(pr.api_url + "/comments?per_page=200", headers=h, timeout=15)
    _raise_for_pr_status(r2, pr=pr)
    return r1.json(), r2.json()
