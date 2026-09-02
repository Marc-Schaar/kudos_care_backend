from django.urls import path

from .views import (
    ActivityDetailView,
    ActivityListView,
    ActivitySummaryView,
    ActivityWearImpactView,
    StravaSyncCancelView,
    StravaSyncStatusView,
    StravaSyncView,
)

urlpatterns = [
    path("strava/sync/", StravaSyncView.as_view()),
    path("strava/sync/cancel/", StravaSyncCancelView.as_view()),
    path("strava/sync-status/", StravaSyncStatusView.as_view()),
    path("activities/", ActivityListView.as_view(), name="activity-list"),
    path("activities/<int:id>/", ActivityDetailView.as_view(), name="activity-detail"),
    path(
        "activities/<int:id>/summary/",
        ActivitySummaryView.as_view(),
        name="activity-summary",
    ),
    path(
        "activities/<int:id>/wear-impact/",
        ActivityWearImpactView.as_view(),
        name="activity-wear-impact",
    ),
]
