from celery import shared_task
import requests
from app_auth.models import StravaProfile
from app_dashboard.models import Ride
from app_dashboard.api.services import StravaImportService

@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def process_strava_webhook(self, data):
    activity_id = data.get('object_id')
    athlete_id = data.get('owner_id')
    event_type = data.get('aspect_type')  
    if event_type == 'delete':
        deleted_count, _ = Ride.objects.filter(
            strava_id=activity_id, 
            athlete__strava_athlete_id=athlete_id
        ).delete()
        
        if deleted_count > 0:
            return f"Aktivität {activity_id} erfolgreich gelöscht."
        return f"Aktivität {activity_id} nicht gefunden, nichts zu löschen."

    if event_type == 'create':
        try:
            if not activity_id or event_type != 'create':
                return "Kein Import nötig"

            profile = StravaProfile.objects.get(strava_athlete_id=athlete_id)

            response = requests.get(
                f"https://www.strava.com/api/v3/activities/{activity_id}",
                headers={"Authorization": f"Bearer {profile.access_token}"},
                timeout=10,
            )
            response.raise_for_status()

            StravaImportService.sync_activity_to_db(
                response.json(),
                access_token=profile.access_token,
            )
            return f"Aktivität {activity_id} importiert"

        except Exception as exc:
            raise self.retry(exc=exc)