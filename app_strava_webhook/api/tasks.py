from celery import shared_task
from app_auth.models import StravaProfile
from app_auth.api.utils import strava_get
from app_dashboard.models import Ride
from app_dashboard.api.services import StravaImportService


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def process_strava_webhook(self, data):
    activity_id = data.get("object_id")
    athlete_id = data.get("owner_id")
    event_type = data.get("aspect_type")
    if event_type == "delete":
        deleted_count, _ = Ride.objects.filter(
            strava_id=activity_id, athlete__strava_athlete_id=athlete_id
        ).delete()

        if deleted_count > 0:
            return f"Aktivität {activity_id} erfolgreich gelöscht."
        return f"Aktivität {activity_id} nicht gefunden, nichts zu löschen."

    if event_type == "create":
        if not activity_id or event_type != "create":
            return "Kein Import nötig"

        try:
            profile = StravaProfile.objects.get(strava_athlete_id=athlete_id)
        except StravaProfile.DoesNotExist:
            return f"Kein StravaProfile für Athlet {athlete_id} gefunden, kein Import möglich."

        try:
            response = strava_get(
                profile,
                f"https://www.strava.com/api/v3/activities/{activity_id}",
                timeout=10,
            )

            if response.status_code == 404:
                return f"Aktivität {activity_id} bei Strava nicht gefunden, kein Import möglich."

            response.raise_for_status()

            StravaImportService.sync_activity_to_db(
                response.json(),
                profile,
            )
            return f"Aktivität {activity_id} importiert"

        except Exception as exc:
            raise self.retry(exc=exc)
