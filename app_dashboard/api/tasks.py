import logging

import requests
from celery import shared_task
from django.utils import timezone

from app_auth.models import StravaProfile
from .services import StravaSyncService

logger = logging.getLogger("my_app_debug")


@shared_task
def run_strava_sync(profile_id):
    profile = StravaProfile.objects.get(pk=profile_id)

    try:
        count = StravaSyncService.full_sync(profile)
        profile.sync_status = "success"
        profile.last_sync_count = count
        profile.sync_error = ""
    except requests.exceptions.HTTPError as e:
        profile.sync_status = "error"
        status_code = e.response.status_code if e.response is not None else None
        if status_code == 403:
            profile.sync_error = "Strava-Zugriff unzureichend. Bitte Strava-Konto neu verbinden."
        else:
            profile.sync_error = "Synchronisation mit Strava fehlgeschlagen"
        logger.exception(
            "Strava-Sync für Athlet %s fehlgeschlagen (Strava-API-Fehler).",
            profile.strava_athlete_id,
        )
    except Exception:
        profile.sync_status = "error"
        profile.sync_error = "Synchronisation fehlgeschlagen"
        logger.exception(
            "Strava-Sync für Athlet %s fehlgeschlagen.", profile.strava_athlete_id
        )
    finally:
        profile.sync_finished_at = timezone.now()
        profile.save(
            update_fields=["sync_status", "sync_finished_at", "sync_error", "last_sync_count"]
        )
