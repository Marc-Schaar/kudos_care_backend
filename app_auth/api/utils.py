import time
import requests
from django.conf import settings

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