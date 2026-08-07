from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from app_auth.models import StravaProfile


def _strava_token_response(athlete_id):
    resp = Mock()
    resp.status_code = 200
    resp.json.return_value = {
        "access_token": "at",
        "refresh_token": "rt",
        "expires_at": 0,
        "athlete": {"id": athlete_id, "firstname": "Test", "lastname": "User"},
    }
    return resp


class StravaAuthCallbackWelcomeEmailTests(APITestCase):
    """
    Deckt den in app_notifications hinzugefügten Auto-Trigger ab: die Willkommens-E-Mail
    wird nur bei Erstanlage eines Profils ausgelöst, nicht bei jedem Re-Login.
    """

    @patch("app_auth.api.views.send_welcome_email_task.delay")
    @patch("app_auth.api.views.sync_bikes_from_strava")
    @patch("app_auth.api.views.requests.post")
    def test_sends_welcome_email_for_newly_created_profile(
        self, mock_post, mock_sync_bikes, mock_delay
    ):
        athlete_id = 70001
        mock_post.return_value = _strava_token_response(athlete_id)

        response = self.client.post("/api/strava/auth/", {"code": "abc123"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        profile = StravaProfile.objects.get(strava_athlete_id=athlete_id)
        mock_delay.assert_called_once_with(profile.id)

    @patch("app_auth.api.views.send_welcome_email_task.delay")
    @patch("app_auth.api.views.requests.post")
    def test_does_not_send_welcome_email_on_relogin(self, mock_post, mock_delay):
        user = get_user_model().objects.create_user(
            username="strava_70002", password="pw"
        )
        StravaProfile.objects.create(
            user=user,
            strava_athlete_id=70002,
            access_token="old",
            refresh_token="old",
            expires_at=0,
        )
        mock_post.return_value = _strava_token_response(70002)

        response = self.client.post("/api/strava/auth/", {"code": "abc123"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_delay.assert_not_called()
