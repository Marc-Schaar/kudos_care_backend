from unittest.mock import patch

import requests
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from app_auth.models import StravaProfile
from app_dashboard.api.tasks import run_strava_sync
from app_dashboard.api.services import StravaImportService
from app_maintenance.models import Bike, BikeType


def _make_profile(user):
    return StravaProfile.objects.create(
        user=user,
        strava_athlete_id=12345,
        access_token="token",
        refresh_token="refresh",
        expires_at=0,
    )


class StravaSyncViewTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="athlete", password="pw")
        self.profile = _make_profile(self.user)
        self.client.force_login(self.user)
        session = self.client.session
        session["strava_athlete_id"] = self.profile.strava_athlete_id
        session.save()

    @patch("app_dashboard.api.views.run_strava_sync.delay")
    def test_sync_dispatches_task_and_marks_running(self, mock_delay):
        mock_delay.return_value.id = "task-abc-123"

        response = self.client.post("/api/strava/sync/")

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.data["status"], "running")
        mock_delay.assert_called_once_with(self.profile.pk)

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.sync_status, "running")
        self.assertIsNotNone(self.profile.sync_started_at)
        self.assertEqual(self.profile.sync_task_id, "task-abc-123")

    @patch("app_dashboard.api.views.run_strava_sync.delay")
    def test_sync_does_not_dispatch_twice_while_running(self, mock_delay):
        self.profile.sync_status = "running"
        self.profile.save(update_fields=["sync_status"])

        response = self.client.post("/api/strava/sync/")

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_delay.assert_not_called()

    def test_sync_requires_authentication(self):
        self.client.logout()
        response = self.client.post("/api/strava/sync/")
        self.assertIn(
            response.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN)
        )


class StravaSyncStatusViewTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="athlete2", password="pw")
        self.profile = _make_profile(self.user)
        self.client.force_login(self.user)
        session = self.client.session
        session["strava_athlete_id"] = self.profile.strava_athlete_id
        session.save()

    def test_returns_current_sync_status(self):
        self.profile.sync_status = "success"
        self.profile.last_sync_count = 5
        self.profile.save(update_fields=["sync_status", "last_sync_count"])

        response = self.client.get("/api/strava/sync-status/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["sync_status"], "success")
        self.assertEqual(response.data["last_sync_count"], 5)

    def test_requires_authentication(self):
        self.client.logout()
        response = self.client.get("/api/strava/sync-status/")
        self.assertIn(
            response.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN)
        )


class RunStravaSyncTaskTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="athlete3", password="pw")
        self.profile = _make_profile(self.user)
        self.profile.sync_status = "running"
        self.profile.save(update_fields=["sync_status"])

    @patch("app_dashboard.api.tasks.StravaSyncService.full_sync")
    def test_success_updates_profile(self, mock_full_sync):
        mock_full_sync.return_value = 7

        run_strava_sync(self.profile.pk)

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.sync_status, "success")
        self.assertEqual(self.profile.last_sync_count, 7)
        self.assertEqual(self.profile.sync_error, "")
        self.assertIsNotNone(self.profile.sync_finished_at)

    @patch("app_dashboard.api.tasks.StravaSyncService.full_sync")
    def test_forbidden_error_sets_reconnect_message(self, mock_full_sync):
        response = requests.Response()
        response.status_code = 403
        mock_full_sync.side_effect = requests.exceptions.HTTPError(response=response)

        run_strava_sync(self.profile.pk)

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.sync_status, "error")
        self.assertIn("neu verbinden", self.profile.sync_error)

    @patch("app_dashboard.api.tasks.StravaSyncService.full_sync")
    def test_generic_error_sets_error_status(self, mock_full_sync):
        mock_full_sync.side_effect = Exception("boom")

        run_strava_sync(self.profile.pk)

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.sync_status, "error")
        self.assertEqual(self.profile.sync_error, "Synchronisation fehlgeschlagen")

    @patch("app_dashboard.api.tasks.StravaSyncService.full_sync")
    def test_does_not_overwrite_manually_cancelled_status(self, mock_full_sync):
        mock_full_sync.return_value = 3
        # Simuliert eine Race-Condition: der Sync wurde manuell abgebrochen,
        # während der Task noch lief.
        self.profile.sync_status = "cancelled"
        self.profile.sync_error = "Manuell abgebrochen"
        self.profile.save(update_fields=["sync_status", "sync_error"])

        run_strava_sync(self.profile.pk)

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.sync_status, "cancelled")
        self.assertEqual(self.profile.sync_error, "Manuell abgebrochen")


class StravaSyncCancelViewTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="athlete4", password="pw")
        self.profile = _make_profile(self.user)
        self.client.force_login(self.user)
        session = self.client.session
        session["strava_athlete_id"] = self.profile.strava_athlete_id
        session.save()

    @patch("app_dashboard.api.views.AsyncResult")
    def test_cancels_running_sync_and_revokes_task(self, mock_async_result):
        self.profile.sync_status = "running"
        self.profile.sync_task_id = "abc-123"
        self.profile.save(update_fields=["sync_status", "sync_task_id"])

        response = self.client.post("/api/strava/sync/cancel/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "cancelled")
        mock_async_result.assert_called_once_with("abc-123")
        mock_async_result.return_value.revoke.assert_called_once_with(terminate=True)

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.sync_status, "cancelled")
        self.assertEqual(self.profile.sync_error, "Manuell abgebrochen")
        self.assertIsNotNone(self.profile.sync_finished_at)

    @patch("app_dashboard.api.views.AsyncResult")
    def test_noop_when_not_running(self, mock_async_result):
        self.profile.sync_status = "success"
        self.profile.save(update_fields=["sync_status"])

        response = self.client.post("/api/strava/sync/cancel/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "success")
        mock_async_result.assert_not_called()

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.sync_status, "success")

    def test_requires_authentication(self):
        self.client.logout()
        response = self.client.post("/api/strava/sync/cancel/")
        self.assertIn(
            response.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN)
        )


class SyncActivityToDbWeatherWearHookTests(APITestCase):
    """
    Prüft den Hook in StravaImportService.sync_activity_to_db, der nach jedem
    importierten Ride mit zugeordnetem Bike eine Wetter-Verschleiß-Neuberechnung
    für dieses Bike anstößt (siehe app_maintenance/api/tasks.py).
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="athlete5", password="pw")
        self.profile = _make_profile(self.user)

    def _activity_data(self, strava_id, gear_id=None):
        return {
            "id": strava_id,
            "name": "Testfahrt",
            "map": {},
            "start_date_local": "2026-06-01T08:00:00Z",
            "start_date": "2026-06-01T08:00:00Z",
            "start_latlng": [48.1, 11.5],
            "distance": 30000,
            "elapsed_time": 3600,
            "gear_id": gear_id,
        }

    def _empty_weather(self):
        return {
            "hourly": {
                "time": [],
                "temperature_2m": [],
                "wind_speed_10m": [],
                "precipitation": [],
                "wind_direction_10m": [],
            }
        }

    @patch("app_dashboard.api.services.recompute_weather_wear_for_bike.delay")
    @patch("app_dashboard.api.services.StravaStreamService.fetch_activity_streams")
    @patch("app_dashboard.api.services.WeatherService.get_historical_weather")
    def test_enqueues_recompute_when_bike_matched(self, mock_weather, mock_streams, mock_delay):
        bike = Bike.objects.create(
            athlete=self.profile, strava_bike_id="b42", name="Rad", bike_type=BikeType.ROAD
        )
        mock_weather.return_value = self._empty_weather()
        mock_streams.return_value = None

        StravaImportService.sync_activity_to_db(self._activity_data(2001, gear_id="b42"), self.profile)

        mock_delay.assert_called_once_with(bike.pk)

    @patch("app_dashboard.api.services.recompute_weather_wear_for_bike.delay")
    @patch("app_dashboard.api.services.StravaStreamService.fetch_activity_streams")
    @patch("app_dashboard.api.services.WeatherService.get_historical_weather")
    def test_does_not_enqueue_when_no_bike_matched(self, mock_weather, mock_streams, mock_delay):
        mock_weather.return_value = self._empty_weather()
        mock_streams.return_value = None

        StravaImportService.sync_activity_to_db(self._activity_data(2002, gear_id=None), self.profile)

        mock_delay.assert_not_called()
