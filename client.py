"""A small GitHub GraphQL and REST client.

The client knows nothing about the statistics being collected. It owns two
concerns: authentication, and retrying transient failures. Everything above it
works in terms of queries and records.
"""

from __future__ import annotations

import os
import re
import threading
import time
from typing import Any

import requests

API_ROOT = "https://api.github.com"
GRAPHQL_URL = f"{API_ROOT}/graphql"

#: Statuses worth retrying. The interesting one is 403, which GitHub returns for
#: secondary rate limits rather than only for authorization failures, and bursty
#: parallel REST traffic is exactly what provokes it.
RETRY_STATUSES = frozenset({403, 429, 500, 502, 503, 504})

#: GraphQL error types meaning "try again later" rather than "this query is
#: wrong". Anything else is a genuine error and is raised immediately.
RETRYABLE_ERROR_TYPES = frozenset({"RATE_LIMITED", "MAX_NODE_LIMIT_EXCEEDED"})

#: Transport failures worth retrying. A run makes hundreds of requests over
#: several minutes, so a single reset connection is expected rather than
#: exceptional, and letting one abort the whole run wastes everything before it.
TRANSPORT_ERRORS = (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                    requests.exceptions.ChunkedEncodingError)

DEFAULT_ATTEMPTS = 5
DEFAULT_TIMEOUT = 120
MAX_BACKOFF_SECONDS = 60


class GitHubError(Exception):
    """A request failed, or failed to stop failing after every retry."""


#: A commit hash at any length. Trimmed rather than removed: a prefix is enough to
#: look a commit up locally, and the whole hash resolves one through GitHub search.
SHORT_HASH_LENGTH = 8

_REPO_PATH = re.compile(r"/repos/[^/\s]+/[^/\s?]+")
_OWNER_NAME = re.compile(r"\b[\w.-]+/[\w.-]+\b")
_HASH = re.compile(r"\b[0-9a-f]{7,40}\b")


def scrub(text: str) -> str:
    """Remove identifying detail from a message before it is raised.

    Everything raised from here can reach a public, permanent workflow log. A
    transport failure quotes the request URL, an error body quotes the repository
    and the commit it was asked about, and a GraphQL error can quote a repository
    name. This is the only copy; `collect` imports it rather than keeping a second.
    """
    text = _REPO_PATH.sub("/repos/<repository>", text)
    text = _OWNER_NAME.sub("<repository>", text)
    return _HASH.sub(lambda match: match.group()[:SHORT_HASH_LENGTH], text)


def short_hash(oid: str | None) -> str:
    """A commit hash trimmed to a prefix that is not the whole thing."""
    return (oid or "unknown")[:SHORT_HASH_LENGTH]


def token_from_environment() -> str:
    """Read the access token from the environment.

    Only the environment is consulted. Reading a token from a file inside the
    working tree invites it being committed, and a deployment supplies the token
    as a secret in any case.
    """
    for name in ("GITHUB_TOKEN", "ACCESS_TOKEN"):
        token = os.environ.get(name)
        if token:
            return token
    raise GitHubError(
        "no token found: set GITHUB_TOKEN or ACCESS_TOKEN to a classic personal "
        "access token with the read:user and repo scopes, authorized for any "
        "organization whose contributions should be counted")


class GitHubClient:
    """Authenticated client with retry and backoff.

    Sessions are thread-local because `requests.Session` is not documented as
    thread safe and the per-commit file fetch runs several workers in parallel.
    """

    def __init__(self, token: str, *, attempts: int = DEFAULT_ATTEMPTS,
                 user_agent: str = "my-github-stats") -> None:
        self._token = token
        self._attempts = attempts
        self._user_agent = user_agent
        self._local = threading.local()

    @property
    def session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({
                "Authorization": f"token {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": self._user_agent,
            })
            self._local.session = session
        return session

    def graphql(self, query: str, variables: dict[str, Any], label: str) -> dict[str, Any]:
        """Run one GraphQL query and return its `data` object."""
        last = "no response"
        for attempt in range(self._attempts):
            try:
                response = self.session.post(
                    GRAPHQL_URL,
                    json={"query": query, "variables": variables},
                    timeout=DEFAULT_TIMEOUT,
                )
            except TRANSPORT_ERRORS as error:
                if attempt + 1 == self._attempts:
                    raise GitHubError(f"{label}: {scrub(str(error))}") from error
                self._back_off(attempt, None)
                continue
            if response.status_code in RETRY_STATUSES:
                last = f"HTTP {response.status_code}"
                self._back_off(attempt, response)
                continue
            if response.status_code != 200:
                raise GitHubError(f"{label}: HTTP {response.status_code}: "
                                  f"{scrub(response.text[:400])}")

            payload = response.json()
            if errors := payload.get("errors") or []:
                types = {error.get("type", "") for error in errors}
                if types & RETRYABLE_ERROR_TYPES and attempt + 1 < self._attempts:
                    last = "; ".join(sorted(types))
                    self._back_off(attempt, response)
                    continue
                detail = "; ".join(
                    f"{error.get('type', 'unknown')}: {scrub(str(error.get('message')))}"
                    for error in errors)
                raise GitHubError(f"{label}: {detail}")

            data = payload.get("data")
            if data is None:
                raise GitHubError(f"{label}: response contained no data")
            return data

        raise GitHubError(f"{label}: gave up after {self._attempts} attempts, "
                          f"last {last}")

    def rest(self, path: str, params: dict[str, Any] | None = None,
             label: str = "") -> Any:
        """Run one REST GET and return its decoded body.

        The path never reaches an error message, and every message that could quote
        one goes through `scrub`, because these reach public build logs.
        """
        last = "no response"
        for attempt in range(self._attempts):
            try:
                response = self.session.get(f"{API_ROOT}{path}", params=params,
                                            timeout=DEFAULT_TIMEOUT)
            except TRANSPORT_ERRORS as error:
                if attempt + 1 == self._attempts:
                    raise GitHubError(
                        f"{label or 'request'}: {scrub(str(error))}") from error
                self._back_off(attempt, None)
                continue
            if response.status_code in RETRY_STATUSES:
                last = f"HTTP {response.status_code}"
                self._back_off(attempt, response)
                continue
            if response.status_code != 200:
                raise GitHubError(f"{label or 'request'}: HTTP "
                                  f"{response.status_code}: "
                                  f"{scrub(response.text[:200])}")
            return response.json()

        raise GitHubError(f"{label or 'request'}: gave up after "
                          f"{self._attempts} attempts, last {last}")

    def _back_off(self, attempt: int, response: requests.Response | None) -> None:
        """Sleep before retrying, preferring the server's own advice.

        `Retry-After` is honoured when present. Failing that, an exhausted rate
        limit carries a reset timestamp worth waiting for. Otherwise the delay
        doubles per attempt up to a ceiling.

        Returns immediately on the last attempt, where there is nothing left to
        wait for. Sleeping there once cost a minute per failing commit.
        """
        if attempt + 1 >= self._attempts:
            return
        delay = min(2 ** attempt, MAX_BACKOFF_SECONDS)
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                delay = max(delay, int(retry_after))
            elif response.headers.get("X-RateLimit-Remaining") == "0":
                reset = response.headers.get("X-RateLimit-Reset")
                if reset and reset.isdigit():
                    delay = max(delay, int(reset) - int(time.time()) + 1)
        time.sleep(max(delay, 1))
