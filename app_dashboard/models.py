from django.contrib.gis.db import models
from app_auth.models import StravaProfile
from app_maintenance.models import Bike


class Ride(models.Model):
    strava_id = models.BigIntegerField(unique=True)
    name = models.CharField(max_length=255)
    track = models.LineStringField(srid=4326, null=True, blank=True)
    start_latlng = models.PointField(srid=4326, null=True, blank=True)
    weather_data = models.JSONField(null=True, blank=True)
    distance = models.FloatField(null=True, blank=True)
    start_date = models.DateTimeField(null=True, blank=True)
    elapsed_time = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    athlete = models.ForeignKey(
        StravaProfile,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="rides",
    )

    bike = models.ForeignKey(
        Bike,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rides",
    )

    def __str__(self):
        return self.name


class RideStream(models.Model):
    ride = models.OneToOneField(
        "Ride", on_delete=models.CASCADE, related_name="streams"
    )
    latlngs = models.JSONField()
    time_series = models.JSONField()
    avg_headwind = models.FloatField(null=True, blank=True)
