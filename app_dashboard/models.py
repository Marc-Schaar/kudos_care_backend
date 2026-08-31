from typing import TYPE_CHECKING

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

    # Zwischengespeicherte KI-generierte Zusammenfassung der Fahrt (siehe
    # app_dashboard/api/services.py::build_ride_summary_prompt).
    ai_summary = models.TextField(blank=True, default="")
    ai_summary_generated_at = models.DateTimeField(null=True, blank=True)

    # Zwischengespeicherte KI-Erklaerung, was diese Fahrt die Komponenten gekostet hat
    # (siehe app_maintenance/api/services.py::build_ride_wear_impact_prompt). Anders als
    # ai_summary haengt das nicht nur an den unveraenderlichen Ride-Zahlen, sondern auch
    # daran, welche Komponenten zum Fahrtzeitpunkt montiert waren — Staleness daher ueber
    # services.py::ride_wear_impact_is_stale().
    wear_impact_summary = models.TextField(
        blank=True,
        default="",
        help_text="Zwischengespeicherte KI-Erklaerung (Deutsch) des Verschleiss-Anteils dieser Fahrt.",
    )
    wear_impact_generated_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Zeitpunkt der letzten Generierung von wear_impact_summary.",
    )
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

    if TYPE_CHECKING:
        id: int
        bike_id: int | None
        streams: "RideStream"

    def __str__(self):
        return self.name


class RideStream(models.Model):
    ride = models.OneToOneField(
        "Ride", on_delete=models.CASCADE, related_name="streams"
    )
    latlngs = models.JSONField()
    time_series = models.JSONField()
    avg_headwind = models.FloatField(null=True, blank=True)

    if TYPE_CHECKING:
        id: int
