from django.urls import path

from . import views

app_name = "directory"
urlpatterns = [
    path("", views.home, name="home"),
    path("repositories/", views.repositories, name="repositories"),
    path("u/<str:username>/", views.profile, name="profile"),
    path("rankings/", views.rankings, name="rankings"),
    path("source/", views.source, name="source"),
    path("source/license/", views.license_text, name="license"),
]
