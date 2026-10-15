from django.urls import path

from . import views

app_name = "core"
urlpatterns = [
    path("health/", views.health, name="health"),
    path("setup/", views.setup, name="setup"),
    path("manage/settings/", views.configuration, name="configuration"),
    path("account/profile/", views.profile, name="profile"),
    path("account/delete/", views.delete_account, name="delete_account"),
    path("policies/<str:kind>/", views.policy, name="policy"),
    path("manage/markdown-preview/", views.markdown_preview, name="markdown_preview"),
    path("webhooks/<str:kind>/", views.webhook, name="webhook"),
]
