from django.urls import path

from . import views

app_name = "bulletins"
urlpatterns = [
    path("", views.index, name="index"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/receipt/", views.receipt, name="receipt"),
]
