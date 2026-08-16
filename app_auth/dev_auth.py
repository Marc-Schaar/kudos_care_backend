from django.contrib.auth.models import User

from app_auth.models import StravaProfile

# Fake Strava-Athlete-ID fuer den Dev-Mock-Login, weit ausserhalb des Bereichs echter
# Strava-IDs. Geteilt zwischen DevLoginView (app_auth/api/dev_views.py) und dem
# seed_dev_data-Command (app_maintenance), damit beide denselben Fake-Nutzer treffen.
DEV_ATHLETE_ID = 999_999_001


def get_or_create_dev_profile() -> StravaProfile:
    """Legt den festen Dev-Fake-Athleten an/holt ihn, ohne echten Strava-OAuth-Call."""
    user, _ = User.objects.get_or_create(username=f"strava_{DEV_ATHLETE_ID}")
    profile, created = StravaProfile.objects.get_or_create(
        strava_athlete_id=DEV_ATHLETE_ID,
        defaults={
            "user": user,
            "access_token": "dev-fake-access-token",
            "refresh_token": "dev-fake-refresh-token",
            "expires_at": 0,
        },
    )
    if not created and profile.user_id is None:
        profile.user = user
        profile.save(update_fields=["user"])
    return profile
