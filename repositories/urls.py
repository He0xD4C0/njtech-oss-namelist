from django.urls import path

from . import views

app_name = "repositories"
urlpatterns = [
    path("", views.index, name="index"),
    path("add/", views.add, name="add"),
    path("<int:pk>/remove/", views.remove, name="remove"),
    path("<int:pk>/recheck/", views.recheck, name="recheck"),
]
