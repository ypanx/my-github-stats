"""The HTTP client, exercised without a network.

The decisions the client makes on its own: which failures are worth retrying, how
long to wait, how it reads a token, and what it is willing to put in an error
message. The transport itself is faked by installing a session on the thread-local
slot the client reads.
"""

from __future__ import annotations

import pytest

from client import GitHubError, token_from_environment
from client import (
    MAX_BACKOFF_SECONDS,
    RETRY_STATUSES,
    RETRYABLE_ERROR_TYPES,
    GitHubClient,
)


HASH = "a5275c6d4c8f1e2b3a4d5e6f7a8b9c0d1e2f3a4b"


class FakeResponse:
    def __init__(self, headers=None, status_code=200, text="", payload=None):
        self.headers = headers or {}
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self):
        return self._payload


def with_session(client: GitHubClient, response: FakeResponse) -> GitHubClient:
    """Install a transport that answers everything with one response."""
    class Session:
        headers: dict = {}

        def get(self, *a, **k):
            return response

        def post(self, *a, **k):
            return response

    client._local.session = Session()
    return client


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
        monkeypatch.setattr("client.time.sleep", slept.append)
        client = GitHubClient("token")
        for attempt in range(4):
            client._back_off(attempt, None)
        assert slept == sorted(slept)
        assert slept[0] < slept[-1]

    def test_it_is_capped(self, monkeypatch):
        slept: list[float] = []
        monkeypatch.setattr("client.time.sleep", slept.append)
        GitHubClient("token", attempts=30)._back_off(20, None)
        assert slept == [MAX_BACKOFF_SECONDS]

    def test_the_last_attempt_does_not_wait(self, monkeypatch):
        """There is nothing left to wait for. Sleeping here cost up to a minute per
        failing commit, multiplied across the parallel file fetch."""
        slept: list[float] = []
        monkeypatch.setattr("client.time.sleep", slept.append)
        client = GitHubClient("token", attempts=3)
        client._back_off(2, None)
        assert slept == []

    def test_the_servers_own_advice_is_preferred(self, monkeypatch):
        slept: list[float] = []
        monkeypatch.setattr("client.time.sleep", slept.append)
        GitHubClient("token")._back_off(0, FakeResponse({"Retry-After": "30"}))
        assert slept == [30]

    def test_an_exhausted_rate_limit_waits_for_its_reset(self, monkeypatch):
        slept: list[float] = []
        monkeypatch.setattr("client.time.sleep", slept.append)
        monkeypatch.setattr("client.time.time", lambda: 1000)
        GitHubClient("token")._back_off(0, FakeResponse(
            {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1045"}))
        assert slept == [46]



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


class TestErrorMessagesAreSafeForAPublicLog:
    """Every message raised from here can reach a permanent public workflow log.
    Response bodies were once the one path that skipped the scrub."""

    def test_a_rest_error_body_is_scrubbed(self):
        client = with_session(GitHubClient("token"), FakeResponse(
            status_code=422,
            text='{"message":"No commit found for SHA: ' + HASH + '"}'))
        with pytest.raises(GitHubError) as caught:
            client.rest("/repos/acme-corp/secret/commits/" + HASH, label="commit files")
        message = str(caught.value)
        assert HASH not in message
        assert "secret" not in message
        assert "422" in message

    def test_a_graphql_error_body_is_scrubbed(self):
        # 422 rather than 500, so this raises on the first attempt instead of
        # spending the retry schedule to reach the same message.
        client = with_session(GitHubClient("token"), FakeResponse(
            status_code=422, text="acme-corp/secret exploded at " + HASH))
        with pytest.raises(GitHubError) as caught:
            client.graphql("query {}", {}, "contributions")
        message = str(caught.value)
        assert HASH not in message
        assert "secret" not in message

    def test_a_rest_path_never_reaches_the_message(self):
        client = with_session(GitHubClient("token"),
                             FakeResponse(status_code=404, text="Not Found"))
        with pytest.raises(GitHubError) as caught:
            client.rest("/repos/acme-corp/secret/commits/abc", label="commit files")
        assert "acme-corp" not in str(caught.value)

    def test_giving_up_names_what_it_gave_up_on(self, monkeypatch):
        """`gave up after 5 attempts` alone is the least diagnosable message the
        module can produce."""
        monkeypatch.setattr("client.time.sleep", lambda _: None)
        client = with_session(GitHubClient("token", attempts=2),
                             FakeResponse(status_code=503, text=""))
        with pytest.raises(GitHubError, match="gave up after 2 attempts, last HTTP 503"):
            client.rest("/repos/a/b/commits/c", label="commit files")
