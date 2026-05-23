from django.urls import path
from .views import StravaBikesView, StravaActivitiesView


urlpatterns = [
      path('strava/bikes/<int:athlete_id>/', StravaBikesView.as_view(), name='bikes-list'),
      path('strava/activities/', StravaActivitiesView.as_view(), name='activities-list'),
      ]