"""A small GitHub GraphQL and REST client.

The client knows nothing about the statistics being collected. It owns exactly
three concerns: authentication, retrying transient failures, and counting what
each run costs. Everything above it works in terms of queries and records.
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


def _scrub(text: str) -> str:
    """Strip repository paths out of a message before it is raised.

    A transport failure quotes the request URL and a GraphQL failure can quote a
    repository name, and both end up in logs that may be public.
    """
    text = _REPO_PATH.sub("/repos/<repository>", text)
    return _OWNER_NAME.sub("<repository>", text)


_REPO_PATH = re.compile(r"/repos/[^/\s]+/[^/\s?]+")
_OWNER_NAME = re.compile(r"\b[\w.-]+/[\w.-]+\b")


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
    """Authenticated client with retry, backoff, and cost accounting.

    Sessions are thread-local because `requests.Session` is not documented as
    thread safe and the per-commit file fetch runs several workers in parallel.
    """

    def __init__(self, token: str, *, attempts: int = DEFAULT_ATTEMPTS,
                 user_agent: str = "my-github-stats") -> None:
        self._token = token
        self._attempts = attempts
        self._user_agent = user_agent
        self._local = threading.local()
        self._lock = threading.Lock()
        self.graphql_points = 0
        self.rest_calls = 0
        self.retries = 0

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
        """Run one GraphQL query and return its `data` object.

        A query that selects `rateLimit { cost }` has its cost added to the
        running total, which is what makes the budget report meaningful.
        """
        for attempt in range(self._attempts):
            try:
                response = self.session.post(
                    GRAPHQL_URL,
                    json={"query": query, "variables": variables},
                    timeout=DEFAULT_TIMEOUT,
                )
            except TRANSPORT_ERRORS as error:
                if attempt + 1 == self._attempts:
                    raise GitHubError(f"{label}: {_scrub(str(error))}") from error
                self._back_off(attempt, None)
                continue
            if response.status_code in RETRY_STATUSES:
                self._back_off(attempt, response)
                continue
            if response.status_code != 200:
                raise GitHubError(
                    f"{label}: HTTP {response.status_code}: {response.text[:400]}")

            payload = response.json()
            if errors := payload.get("errors") or []:
                types = {error.get("type", "") for error in errors}
                if types & RETRYABLE_ERROR_TYPES and attempt + 1 < self._attempts:
                    self._back_off(attempt, response)
                    continue
                detail = "; ".join(
                    f"{error.get('type', 'unknown')}: {_scrub(str(error.get('message')))}"
                    for error in errors)
                raise GitHubError(f"{label}: {detail}")

            data = payload.get("data")
            if data is None:
                raise GitHubError(f"{label}: response contained no data")
            self._record_cost(data)
            return data

        raise GitHubError(f"{label}: gave up after {self._attempts} attempts")

    def rest(self, path: str, params: dict[str, Any] | None = None,
             label: str = "") -> Any:
        """Run one REST GET and return its decoded body.

        The path is deliberately kept out of error messages, because it carries
        the repository owner and name and these messages reach public build logs.
        """
        for attempt in range(self._attempts):
            try:
                response = self.session.get(f"{API_ROOT}{path}", params=params,
                                            timeout=DEFAULT_TIMEOUT)
            except TRANSPORT_ERRORS as error:
                if attempt + 1 == self._attempts:
                    raise GitHubError(
                        f"{label or 'request'}: {_scrub(str(error))}") from error
                self._back_off(attempt, None)
                continue
            with self._lock:
                self.rest_calls += 1
            if response.status_code in RETRY_STATUSES:
                self._back_off(attempt, response)
                continue
            if response.status_code != 200:
                raise GitHubError(
                    f"{label or 'request'}: HTTP {response.status_code}: "
                    f"{response.text[:200]}")
            return response.json()

        raise GitHubError(f"{label or 'request'}: gave up after {self._attempts} attempts")

    def budget(self) -> str:
        """One line describing what this run has cost so far."""
        return (f"{self.graphql_points} GraphQL points, {self.rest_calls} REST "
                f"requests, {self.retries} retries")

    def _record_cost(self, data: dict[str, Any]) -> None:
        rate_limit = data.get("rateLimit") if isinstance(data, dict) else None
        cost = (rate_limit or {}).get("cost") if isinstance(rate_limit, dict) else None
        with self._lock:
            self.graphql_points += int(cost or 1)

    def _back_off(self, attempt: int, response: requests.Response | None) -> None:
        """Sleep before retrying, preferring the server's own advice.

        `Retry-After` is honoured when present. Failing that, an exhausted rate
        limit carries a reset timestamp worth waiting for. Otherwise the delay
        doubles per attempt up to a ceiling.
        """
        delay = min(2 ** attempt, MAX_BACKOFF_SECONDS)
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                delay = max(delay, int(retry_after))
            elif response.headers.get("X-RateLimit-Remaining") == "0":
                reset = response.headers.get("X-RateLimit-Reset")
                if reset and reset.isdigit():
                    delay = max(delay, int(reset) - int(time.time()) + 1)
        with self._lock:
            self.retries += 1
        time.sleep(max(delay, 1))
