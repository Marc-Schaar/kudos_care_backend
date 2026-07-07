from django.urls import path
from .views import StravaSyncView, StravaSyncStatusView, ActivityListView, ActivityDetailView

urlpatterns = [
    path("strava/sync/", StravaSyncView.as_view()),
    path("strava/sync-status/", StravaSyncStatusView.as_view()),
    path("activities/", ActivityListView.as_view(), name="activity-list"),
    path("activities/<int:id>/", ActivityDetailView.as_view(), name="activity-detail"),
]
