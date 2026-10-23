"""Minimal GitHub REST client: JSON reads, paginated lists and comment writes.

Every expected failure (not found, bad token, rate limit, network) becomes a
`GitHubError` with a hint, so the CLI never shows a raw traceback for them.
"""

import os
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import httpx2 as httpx

from review_map import __version__
from review_map.errors import ReviewMapError

API_URL = "https://api.github.com"
ENV_TOKEN = "GITHUB_TOKEN"
TIMEOUT_SECONDS = 20.0
MAX_PAGES = 30  # 3,000 items: far more comments than a PR plausibly has

WRITE_HINT = (
    "Give the workflow `permissions: pull-requests: write`. PRs from forks only get a "
    "read-only token, so they can't be commented on."
)

TOKEN_URL = "https://github.com/settings/personal-access-tokens/new"


def token_hint(private: bool = False) -> str:
    """One-line onboarding hint for GITHUB_TOKEN, in the style of the LLM `setup_hint()`.

    Shown only when a token would actually help: a rate limit or a private repo.
    """
    if private:
        return (
            "If the repo is private, set GITHUB_TOKEN in .env (see .env.example). "
            f"Create one at {TOKEN_URL} with read-only Contents and Pull requests access."
        )
    return (
        "Set GITHUB_TOKEN in .env to raise the limit to 5,000 requests an hour "
        f"(see .env.example). Create one at {TOKEN_URL}; no extra permissions needed."
    )


class GitHubError(ReviewMapError):
    """A GitHub API call failed in an expected way."""


class GitHubPermissionError(GitHubError):
    """The token may read but not write (missing permission, or a PR from a fork)."""


def token_from_env(env: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if env is None else env
    return env.get(ENV_TOKEN, "").strip() or None


def _reset_time(response: httpx.Response) -> str | None:
    """When the rate limit resets, as local HH:MM, from `X-RateLimit-Reset` (epoch seconds)."""
    try:
        reset = int(response.headers["x-ratelimit-reset"])
    except (KeyError, ValueError):
        return None
    return datetime.fromtimestamp(reset).astimezone().strftime("%H:%M")


def _api_message(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return ""
    return str(data.get("message", "")) if isinstance(data, dict) else ""


def _is_rate_limited(response: httpx.Response) -> bool:
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    if response.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in response.headers:
        return True
    return "rate limit" in _api_message(response).lower()


class GitHubClient:
    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str = API_URL,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.token = token
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"review-map/{__version__}",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.Client(
            base_url=base_url,
            headers=headers,
            timeout=TIMEOUT_SECONDS,
            transport=transport,
            follow_redirects=True,  # renamed repos answer with a 301
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _request(
        self, method: str, url: str, *, what: str, json: Any = None, write: bool = False
    ) -> httpx.Response:
        try:
            response = self._http.request(method, url, json=json)
        except httpx.TimeoutException:
            raise GitHubError(
                "GitHub did not answer in time.", hint="Check your connection and try again."
            ) from None
        except httpx.TransportError as exc:
            raise GitHubError(
                f"Could not reach GitHub: {exc}", hint="Check your connection and try again."
            ) from None
        self._raise_for_status(response, what, write=write)
        return response

    @staticmethod
    def _json(response: httpx.Response, what: str) -> Any:
        try:
            return response.json()
        except ValueError:
            raise GitHubError(f"GitHub returned an unreadable answer for {what}.") from None

    def get_json(self, path: str, *, what: str) -> Any:
        """GET `path` and return its JSON. `what` names the thing for error messages."""
        return self._json(self._request("GET", path, what=what), what)

    def get_all(self, path: str, *, what: str, max_pages: int = MAX_PAGES) -> list[Any]:
        """GET a list endpoint, following `Link: rel="next"` pages (100 items each)."""
        items: list[Any] = []
        url: str | None = f"{path}{'&' if '?' in path else '?'}per_page=100"
        for _ in range(max_pages):
            if url is None:
                break
            response = self._request("GET", url, what=what)
            page = self._json(response, what)
            if not isinstance(page, list):
                raise GitHubError(f"GitHub returned an unexpected answer for {what}.")
            items += page
            url = response.links.get("next", {}).get("url")
            if url and httpx.URL(url).host != self._http.base_url.host:
                raise GitHubError(f"GitHub sent a next-page link to another host for {what}.")
        return items

    def post_json(self, path: str, body: Any, *, what: str) -> Any:
        return self._json(self._request("POST", path, what=what, json=body, write=True), what)

    def patch_json(self, path: str, body: Any, *, what: str) -> Any:
        return self._json(self._request("PATCH", path, what=what, json=body, write=True), what)

    def login(self) -> str | None:
        """The token's user login, or None when GitHub won't say (e.g. an Actions token)."""
        if not self.token:
            return None
        try:
            data = self.get_json("/user", what="the token's user")
        except GitHubError:
            return None
        return data.get("login") if isinstance(data, dict) else None

    def _raise_for_status(
        self, response: httpx.Response, what: str, *, write: bool = False
    ) -> None:
        status = response.status_code
        if status < 400:
            return
        if _is_rate_limited(response):
            reset = _reset_time(response)
            when = f" It resets at {reset}." if reset else ""
            if self.token:
                hint = "Wait for the reset and try again."
            else:
                hint = f"Without a token GitHub allows 60 requests an hour. {token_hint()}"
            raise GitHubError(f"GitHub rate limit reached.{when}", hint=hint)
        if status == 401:
            raise GitHubError(
                "GitHub rejected the token (401 Unauthorized).",
                hint="Check GITHUB_TOKEN: it may be expired, revoked or mistyped.",
            )
        if status == 404:
            if self.token:
                raise GitHubError(
                    f"Not found on GitHub: {what}.",
                    hint="Check the name and number, and that your token can read this repo.",
                )
            raise GitHubError(
                f"Not found on GitHub: {what}.",
                hint=f"Check the name and number. {token_hint(private=True)}",
            )
        if status == 403:
            detail = _api_message(response)
            message = f"GitHub refused access to {what} (403)" + (f": {detail}" if detail else ".")
            if write:
                raise GitHubPermissionError(message, hint=WRITE_HINT)
            raise GitHubError(message, hint="Check that your token has read access to this repo.")
        if status >= 500:
            raise GitHubError(
                f"GitHub had a server error ({status}).", hint="Try again in a minute."
            )
        detail = _api_message(response)
        raise GitHubError(
            f"GitHub request for {what} failed ({status})" + (f": {detail}" if detail else ".")
        )
