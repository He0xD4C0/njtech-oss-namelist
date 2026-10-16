"""Stable provider service interface used by repository workflows."""
from .clients import (
    GiteeClient,
    GitHubClient,
    GitLabClient,
    Identity,
    ProviderUnavailable,
    RepoData,
    RepositoryIneligible,
    get_client,
    parse_repository_url,
    validate_repository,
)

__all__ = [
    "GitHubClient",
    "GitLabClient",
    "GiteeClient",
    "Identity",
    "ProviderUnavailable",
    "RepoData",
    "RepositoryIneligible",
    "get_client",
    "parse_repository_url",
    "validate_repository",
]

