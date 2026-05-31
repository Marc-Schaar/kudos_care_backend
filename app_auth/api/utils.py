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

def get_valid_access_token(profile):
    if profile.expires_at < time.time():
        response = requests.post("https://www.strava.com/oauth/token", data={
            "client_id": settings.STRAVA_CLIENT_ID,
            "client_secret": settings.STRAVA_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": profile.refresh_token,
        })
        data = response.json()
        profile.access_token = data["access_token"]
        profile.refresh_token = data["refresh_token"]
        profile.expires_at = data["expires_at"]
        profile.save()
    return profile.access_token