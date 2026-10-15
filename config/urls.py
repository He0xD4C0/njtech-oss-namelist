from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.urls import include, path

from providers.views import connections_compat

admin.site.login = login_required(admin.site.login)
urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/3rdparty/", connections_compat, name="socialaccount_connections"),
    path("accounts/", include("allauth.urls")),
    path("i18n/", include("django.conf.urls.i18n")),
    path("", include("core.urls")),
    path("platforms/", include("providers.urls")),
    path("submit/", include("repositories.urls")),
    path("announcements/", include("bulletins.urls")),
    path("", include("directory.urls")),
]
