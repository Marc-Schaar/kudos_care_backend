from django.urls import path
from .views import StravaWebhookView

urlpatterns = [
    path("strava/webhook/", StravaWebhookView.as_view(), name="strava-webhook"),
]
