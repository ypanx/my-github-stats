"""GitHub API access, kept separate from anything that interprets the data."""

from client.client import GitHubClient, GitHubError, token_from_environment

__all__ = ["GitHubClient", "GitHubError", "token_from_environment"]
