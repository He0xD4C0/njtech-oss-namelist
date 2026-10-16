"""Thin extensions of allauth providers: fixed minimal scopes and safe profile retrieval."""
from allauth.socialaccount.adapter import get_adapter
from allauth.socialaccount.providers.github.provider import GitHubProvider as BaseGitHubProvider
from allauth.socialaccount.providers.github.views import GitHubOAuth2Adapter as BaseGitHubOAuth2Adapter
from allauth.socialaccount.providers.gitlab.provider import GitLabProvider as BaseGitLabProvider
from allauth.socialaccount.providers.gitlab.views import GitLabOAuth2Adapter as BaseGitLabOAuth2Adapter

from .gitee.provider import GiteeProvider
from .oauth_views import social_login_from_profile


class MinimalScopeMixin:
    def extract_common_fields(self, data):
        fields = super().extract_common_fields(data)
        fields.pop("email", None)
        return fields

    def extract_email_addresses(self, data):
        return []

    def get_scope_from_request(self, request):
        # Do not permit ?scope=repo/api to broaden permissions dynamically.
        return self.get_scope()

    def get_auth_params_from_request(self, request, action):
        # User-supplied auth_params must not override client_id/redirect/scope.
        return self.get_auth_params()

    def extract_extra_data(self, data):
        return {key: data[key] for key in ("id", "login", "username", "name", "avatar_url", "html_url", "web_url") if key in data}


class GitHubOAuth2Adapter(BaseGitHubOAuth2Adapter):
    access_token_url = "https://github.com/login/oauth/access_token"
    authorize_url = "https://github.com/login/oauth/authorize"

    def complete_login(self, request, app, token, **kwargs):
        # No /user/emails call or user:email scope: email verification is local.
        with get_adapter().get_requests_session() as session:
            response = session.get("https://api.github.com/user", headers={"Authorization": f"Bearer {token.token}"})
            return social_login_from_profile(self, request, response)


class GitLabOAuth2Adapter(BaseGitLabOAuth2Adapter):
    def complete_login(self, request, app, token, **kwargs):
        # allauth's default adapter puts access_token in a query string. Keep
        # credentials out of request URLs, access logs and referrer metadata.
        with get_adapter().get_requests_session() as session:
            response = session.get("https://gitlab.com/api/v4/user", headers={"Authorization": f"Bearer {token.token}"})
            return social_login_from_profile(self, request, response)


class GitHubProvider(MinimalScopeMixin, BaseGitHubProvider):
    oauth2_adapter_class = GitHubOAuth2Adapter

    @classmethod
    def get_package(cls):
        return "providers.github"


class GitLabProvider(MinimalScopeMixin, BaseGitLabProvider):
    oauth2_adapter_class = GitLabOAuth2Adapter

    @classmethod
    def get_package(cls):
        return "providers.gitlab"


provider_classes = [GitHubProvider, GitLabProvider, GiteeProvider]

