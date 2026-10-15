from allauth.socialaccount.adapter import get_adapter
from allauth.socialaccount.providers.oauth2.views import OAuth2Adapter

from providers.oauth_views import CallbackView, LoginView, social_login_from_profile


class GiteeOAuth2Adapter(OAuth2Adapter):
    provider_id = "gitee"
    access_token_url = "https://gitee.com/oauth/token"
    authorize_url = "https://gitee.com/oauth/authorize"
    profile_url = "https://gitee.com/api/v5/user"

    def complete_login(self, request, app, token, **kwargs):
        with get_adapter().get_requests_session() as session:
            response = session.get(self.profile_url, headers={"Authorization": f"Bearer {token.token}"})
            return social_login_from_profile(self, request, response)


oauth2_login = LoginView.adapter_view(GiteeOAuth2Adapter)
oauth2_callback = CallbackView.adapter_view(GiteeOAuth2Adapter)

