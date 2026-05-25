from django.urls import path
from .views import StravaBikesView, StravaSyncView, ActivityListView, ActivityDetailView

urlpatterns = [
    path(
        "strava/bikes/<int:athlete_id>/", StravaBikesView.as_view(), name="bikes-list"
    ),
    path("strava/sync/", StravaSyncView.as_view()),
    path("activities/", ActivityListView.as_view(), name="activity-list"),
    path("activities/<int:id>/", ActivityDetailView.as_view(), name="activity-detail"),
]
