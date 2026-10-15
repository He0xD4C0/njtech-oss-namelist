from providers.oauth_views import CallbackView, LoginView
from providers.provider import GitLabOAuth2Adapter

oauth2_login = LoginView.adapter_view(GitLabOAuth2Adapter)
oauth2_callback = CallbackView.adapter_view(GitLabOAuth2Adapter)

