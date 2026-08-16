from django.conf import settings
from django.urls import path
from .views import StravaAuthCallbackView, CurrentUserView, LogoutView

urlpatterns = [
    path("strava/auth/", StravaAuthCallbackView.as_view(), name="strava-auth"),
    path("strava/me/", CurrentUserView.as_view(), name="strava-me"),
    path("strava/logout/", LogoutView.as_view(), name="logout"),
]

if settings.DEBUG:
    # Nur fuer lokale Entwicklung: Mock-Login ohne echten Strava-OAuth-Roundtrip
    # (siehe dev_views.py / app_auth/dev_auth.py / seed_dev_data-Command).
    from .dev_views import DevLoginView

    urlpatterns.append(path("dev/login/", DevLoginView.as_view(), name="dev-login"))
