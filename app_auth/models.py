from django.db import models
from django.contrib.auth.models import User


class StravaProfile(models.Model):
    SYNC_STATUS_CHOICES = [
        ("idle", "idle"),
        ("running", "running"),
        ("success", "success"),
        ("error", "error"),
        ("cancelled", "cancelled"),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE, null=True, blank=True)

    strava_athlete_id = models.IntegerField(unique=True)

    access_token = models.CharField(max_length=255)
    refresh_token = models.CharField(max_length=255)
    expires_at = models.IntegerField()

    sync_status = models.CharField(max_length=10, choices=SYNC_STATUS_CHOICES, default="idle")
    sync_started_at = models.DateTimeField(null=True, blank=True)
    sync_finished_at = models.DateTimeField(null=True, blank=True)
    sync_error = models.CharField(max_length=255, blank=True, default="")
    last_sync_count = models.IntegerField(null=True, blank=True)
    sync_task_id = models.CharField(max_length=255, blank=True, default="")
    sync_progress_current = models.IntegerField(null=True, blank=True)
    sync_progress_total = models.IntegerField(null=True, blank=True)

    def __str__(self):
        return f"Strava ID: {self.strava_athlete_id}"
