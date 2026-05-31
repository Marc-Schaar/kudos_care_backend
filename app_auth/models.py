from django.db import models
from django.contrib.auth.models import User


class StravaProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, null=True, blank=True)

    strava_athlete_id = models.IntegerField(unique=True)
    firstname = models.CharField(max_length=100, blank=True)
    lastname = models.CharField(max_length=100, blank=True)

    access_token = models.CharField(max_length=255)
    refresh_token = models.CharField(max_length=255)
    expires_at = models.IntegerField()

    def __str__(self):
        return f"{self.firstname} {self.lastname} (Strava ID: {self.strava_athlete_id})"
