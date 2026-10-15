from allauth.socialaccount.providers.oauth2.urls import default_urlpatterns

from providers.provider import GitLabProvider

urlpatterns = default_urlpatterns(GitLabProvider)

