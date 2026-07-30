"""The HTTP client, exercised without a network.

Only the decisions the client makes on its own are tested here: which failures
are worth retrying, how long to wait, and how it reads a token. Everything else
it does is delegate to the transport.
"""

from __future__ import annotations

import pytest

from client import GitHubError, token_from_environment
from client.client import (
    MAX_BACKOFF_SECONDS,
    RETRY_STATUSES,
    RETRYABLE_ERROR_TYPES,
    GitHubClient,
)


class FakeResponse:
    def __init__(self, headers=None):
        self.headers = headers or {}


class TestToken:
    def test_the_preferred_variable_wins(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "first")
        monkeypatch.setenv("ACCESS_TOKEN", "second")
        assert token_from_environment() == "first"

    def test_the_fallback_variable_is_accepted(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("ACCESS_TOKEN", "second")
        assert token_from_environment() == "second"

    def test_a_missing_token_explains_what_is_needed(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("ACCESS_TOKEN", raising=False)
        with pytest.raises(GitHubError, match="read:user"):
            token_from_environment()

    def test_an_empty_token_is_not_accepted(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "")
        monkeypatch.delenv("ACCESS_TOKEN", raising=False)
        with pytest.raises(GitHubError):
            token_from_environment()


class TestRetryPolicy:
    def test_secondary_rate_limits_are_retried(self):
        """GitHub reports these as a forbidden response rather than as a rate
        limit, and parallel requests are what provoke them."""
        assert 403 in RETRY_STATUSES

    def test_transient_server_failures_are_retried(self):
        assert {429, 500, 502, 503, 504} <= RETRY_STATUSES

    def test_client_mistakes_are_not_retried(self):
        """Retrying a malformed query would waste the budget and still fail."""
        assert not {400, 401, 404, 422} & RETRY_STATUSES

    def test_only_transient_query_errors_are_retried(self):
        assert "RATE_LIMITED" in RETRYABLE_ERROR_TYPES
        assert "NOT_FOUND" not in RETRYABLE_ERROR_TYPES


class TestBackoff:
    def test_it_grows_with_each_attempt(self, monkeypatch):
        slept: list[float] = []
        monkeypatch.setattr("client.client.time.sleep", slept.append)
        client = GitHubClient("token")
        for attempt in range(4):
            client._back_off(attempt, None)
        assert slept == sorted(slept)
        assert slept[0] < slept[-1]

    def test_it_is_capped(self, monkeypatch):
        slept: list[float] = []
        monkeypatch.setattr("client.client.time.sleep", slept.append)
        GitHubClient("token")._back_off(20, None)
        assert slept == [MAX_BACKOFF_SECONDS]

    def test_the_servers_own_advice_is_preferred(self, monkeypatch):
        slept: list[float] = []
        monkeypatch.setattr("client.client.time.sleep", slept.append)
        GitHubClient("token")._back_off(0, FakeResponse({"Retry-After": "30"}))
        assert slept == [30]

    def test_an_exhausted_rate_limit_waits_for_its_reset(self, monkeypatch):
        slept: list[float] = []
        monkeypatch.setattr("client.client.time.sleep", slept.append)
        monkeypatch.setattr("client.client.time.time", lambda: 1000)
        GitHubClient("token")._back_off(0, FakeResponse(
            {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1045"}))
        assert slept == [46]

    def test_every_wait_is_counted(self, monkeypatch):
        monkeypatch.setattr("client.client.time.sleep", lambda _: None)
        client = GitHubClient("token")
        for attempt in range(3):
            client._back_off(attempt, None)
        assert client.retries == 3


class TestAccounting:
    def test_a_reported_cost_is_accumulated(self):
        client = GitHubClient("token")
        client._record_cost({"rateLimit": {"cost": 35}})
        client._record_cost({"rateLimit": {"cost": 7}})
        assert client.graphql_points == 42

    def test_an_unreported_cost_counts_as_one(self):
        client = GitHubClient("token")
        client._record_cost({})
        assert client.graphql_points == 1

    def test_the_budget_reads_as_a_sentence(self):
        client = GitHubClient("token")
        client._record_cost({"rateLimit": {"cost": 5}})
        assert "GraphQL points" in client.budget()


class TestSession:
    def test_it_authenticates_and_identifies_itself(self):
        session = GitHubClient("secret", user_agent="tests").session
        assert session.headers["Authorization"] == "token secret"
        assert session.headers["User-Agent"] == "tests"
        assert "X-GitHub-Api-Version" in session.headers

    def test_it_is_reused_within_a_thread(self):
        """A new session per request would discard connection pooling."""
        client = GitHubClient("token")
        assert client.session is client.session
