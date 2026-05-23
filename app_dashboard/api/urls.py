from django.urls import path
from .views import StravaBikesView


urlpatterns = [
      path('strava/bikes/<int:athlete_id>/', StravaBikesView.as_view(), name='bikes-list'),]