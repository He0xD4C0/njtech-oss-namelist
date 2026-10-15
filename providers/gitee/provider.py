from allauth.socialaccount.providers.base import ProviderAccount
from allauth.socialaccount.providers.oauth2.provider import OAuth2Provider

from .views import GiteeOAuth2Adapter


class GiteeAccount(ProviderAccount):
    def get_profile_url(self):
        return self.account.extra_data.get("html_url")

    def get_avatar_url(self):
        return self.account.extra_data.get("avatar_url")


class GiteeProvider(OAuth2Provider):
    id = "gitee"
    name = "Gitee"
    account_class = GiteeAccount
    oauth2_adapter_class = GiteeOAuth2Adapter

    def get_default_scope(self):
        return ["user_info"]

    def get_scope_from_request(self, request):
        return self.get_scope()

    def get_auth_params_from_request(self, request, action):
        return self.get_auth_params()

    def extract_uid(self, data):
        return str(data["id"])

    def extract_common_fields(self, data):
        return {"username": data.get("login"), "name": data.get("name")}

    def extract_extra_data(self, data):
        return {key: data[key] for key in ("id", "login", "name", "avatar_url", "html_url") if key in data}


provider_classes = [GiteeProvider]

