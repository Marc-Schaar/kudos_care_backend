from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.utils import timezone
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


class CurrentUserSettingsTests(APITestCase):
    """
    GET/PATCH /api/strava/me/ — die Basis fuer Usermenue und E-Mail-Dialog.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="settings-user")
        self.profile = StravaProfile.objects.create(
            user=self.user,
            strava_athlete_id=112233,
            access_token="t",
            refresh_token="r",
            expires_at=0,
        )
        self.client.force_login(self.user)
        session = self.client.session
        session["strava_athlete_id"] = self.profile.strava_athlete_id
        session.save()

    def test_get_reports_needs_email_when_none_stored(self):
        response = self.client.get("/api/strava/me/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["needs_email"])
        self.assertEqual(response.data["email"], "")
        self.assertTrue(response.data["email_notifications_enabled"])

    @patch("app_auth.api.views.send_welcome_email_task.delay")
    def test_patch_stores_email_and_clears_needs_email(self, mock_delay):
        response = self.client.patch(
            "/api/strava/me/", {"email": "marc@example.de"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["needs_email"])
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "marc@example.de")

    @patch("app_auth.api.views.send_welcome_email_task.delay")
    def test_first_email_triggers_the_missed_welcome_mail(self, mock_delay):
        """
        Ohne Adresse lief die Willkommens-Mail bei der Erstanmeldung ins Leere —
        sobald eine da ist, wird sie nachgeholt.
        """
        self.client.patch(
            "/api/strava/me/", {"email": "marc@example.de"}, format="json"
        )
        mock_delay.assert_called_once_with(self.profile.id)

    @patch("app_auth.api.views.send_welcome_email_task.delay")
    def test_changing_an_existing_email_does_not_resend_welcome(self, mock_delay):
        self.user.email = "alt@example.de"
        self.user.save()

        self.client.patch("/api/strava/me/", {"email": "neu@example.de"}, format="json")
        mock_delay.assert_not_called()

    @patch("app_auth.api.views.send_welcome_email_task.delay")
    def test_welcome_is_not_resent_when_already_sent(self, mock_delay):
        self.profile.welcome_email_sent_at = timezone.now()
        self.profile.save()

        self.client.patch(
            "/api/strava/me/", {"email": "marc@example.de"}, format="json"
        )
        mock_delay.assert_not_called()

    def test_patch_toggles_notifications(self):
        response = self.client.patch(
            "/api/strava/me/", {"email_notifications_enabled": False}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["email_notifications_enabled"])
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.email_notifications_enabled)

    def test_invalid_email_is_rejected(self):
        response = self.client.patch(
            "/api/strava/me/", {"email": "kein-at-zeichen"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "")

    def test_empty_body_is_rejected(self):
        response = self.client.patch("/api/strava/me/", {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_patch_requires_a_session(self):
        self.client.logout()
        response = self.client.patch(
            "/api/strava/me/", {"email": "marc@example.de"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
