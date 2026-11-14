import time

import requests
from django.conf import settings

from app_maintenance.models import Bike, BikeType


def sync_bikes_from_strava(athlete_data, profile):
    for bike_data in athlete_data.get("bikes", []):
        Bike.objects.update_or_create(
            strava_bike_id=bike_data["id"],
            defaults={
                "athlete": profile,
                "name": bike_data.get("name", "Unbekanntes Rad"),
                "bike_type": BikeType.OTHER,
            },
        )


def get_valid_access_token(profile, force=False):
    if force or profile.expires_at < time.time():
        response = requests.post(
            "https://www.strava.com/oauth/token",
            data={
                "client_id": settings.STRAVA_CLIENT_ID,
                "client_secret": settings.STRAVA_CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": profile.refresh_token,
            },
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        profile.access_token = data["access_token"]
        profile.refresh_token = data["refresh_token"]
        profile.expires_at = data["expires_at"]
        profile.save()
    return profile.access_token


def strava_request(profile, method, url, timeout=10, **kwargs):
    """
    Request gegen die Strava-API. Antwortet Strava trotz lokal noch gültigem
    expires_at mit 401 (Token wurde serverseitig invalidiert), wird der
    Access-Token einmalig zwangsweise neu geholt und der Request wiederholt.
    Dateien in `files` müssen dafür als Bytes vorliegen, nicht als Stream.
    """
    access_token = get_valid_access_token(profile)
    response = requests.request(
        method,
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=timeout,
        **kwargs,
    )

    if response.status_code == 401:
        access_token = get_valid_access_token(profile, force=True)
        response = requests.request(
            method,
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=timeout,
            **kwargs,
        )

    return response


def strava_get(profile, url, params=None, timeout=10):
    """GET-Request gegen die Strava-API, mit einmaligem Token-Refresh bei 401."""
    return strava_request(profile, "GET", url, params=params, timeout=timeout)
