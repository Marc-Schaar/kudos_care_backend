from typing import TYPE_CHECKING

from django.contrib.auth.models import User
from django.db import models


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

    sync_status = models.CharField(
        max_length=10, choices=SYNC_STATUS_CHOICES, default="idle"
    )
    sync_started_at = models.DateTimeField(null=True, blank=True)
    sync_finished_at = models.DateTimeField(null=True, blank=True)
    sync_error = models.CharField(max_length=255, blank=True, default="")
    last_sync_count = models.IntegerField(null=True, blank=True)
    sync_task_id = models.CharField(max_length=255, blank=True, default="")
    sync_progress_current = models.IntegerField(null=True, blank=True)
    sync_progress_total = models.IntegerField(null=True, blank=True)

    # Benachrichtigungen (siehe app_notifications) — Kill-Switch fuer alle automatischen
    # E-Mails. Vom Nutzer selbst ueber das Usermenue schaltbar
    # (PATCH /api/strava/me/, app_auth/api/views.py::CurrentUserView) und zusaetzlich
    # im Admin.
    email_notifications_enabled = models.BooleanField(
        default=True,
        help_text="Kill-Switch fuer Warn-/Vorhersage-/Willkommens-E-Mails.",
    )
    welcome_email_sent_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "Zeitpunkt des letzten Versands der Willkommens-E-Mail — automatisch gesetzt bei "
            "Erstanmeldung, erneut auslösbar über die Admin-Action 'Willkommens-E-Mail senden'."
        ),
    )

    if TYPE_CHECKING:
        id: int

    def __str__(self):
        return f"Strava ID: {self.strava_athlete_id}"
