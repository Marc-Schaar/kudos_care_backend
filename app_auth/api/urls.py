from django.urls import path
from .views import StravaAuthCallbackView, CurrentUserView, LogoutView

urlpatterns = [
    path("strava/auth/", StravaAuthCallbackView.as_view(), name="strava-auth"),
    path("strava/me/", CurrentUserView.as_view(), name="strava-me"),
    path("strava/logout/", LogoutView.as_view(), name="logout"),
]
