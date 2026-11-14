from unittest.mock import patch

from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework.test import APITestCase

from app_auth.models import StravaProfile
from app_dashboard.api.services import StravaImportService
from app_dashboard.models import Ride
from app_maintenance.models import Bike

ME_URL = "/api/strava/me/"
AUTH_URL = "/api/strava/auth/"


class ConsentTestCase(APITestCase):
    def setUp(self):
        self.user = User.objects.create(username="strava_501")
        self.profile = StravaProfile.objects.create(
            user=self.user,
            strava_athlete_id=501,
            access_token="a",
            refresh_token="r",
            expires_at=0,
        )
        self.client.force_authenticate(self.user)
        session = self.client.session
        session["strava_athlete_id"] = 501
        session.save()


class ConsentSettingsTests(ConsentTestCase):
    def test_defaults_to_no_consent(self):
        data = self.client.get(ME_URL).json()
        self.assertFalse(data["weather_consent"])
        self.assertFalse(data["ai_consent"])

    def test_patch_records_the_moment_of_consent(self):
        data = self.client.patch(ME_URL, {"ai_consent": True}, format="json").json()
        self.assertTrue(data["ai_consent"])
        self.assertFalse(data["weather_consent"])
        self.profile.refresh_from_db()
        self.assertIsNotNone(self.profile.ai_consent_at)

    def test_repeating_consent_keeps_the_original_moment(self):
        first = timezone.now() - timezone.timedelta(days=30)
        StravaProfile.objects.filter(pk=self.profile.pk).update(
            weather_consent_at=first
        )
        self.client.patch(ME_URL, {"weather_consent": True}, format="json")
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.weather_consent_at, first)

    def test_consent_can_be_revoked(self):
        StravaProfile.objects.filter(pk=self.profile.pk).update(
            ai_consent_at=timezone.now()
        )
        data = self.client.patch(ME_URL, {"ai_consent": False}, format="json").json()
        self.assertFalse(data["ai_consent"])
        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.ai_consent_at)


class ConsentAtLoginTests(APITestCase):
    def _login(self, **extra):
        response_data = {
            "access_token": "a",
            "refresh_token": "r",
            "expires_at": 1,
            "athlete": {"id": 777, "firstname": "A", "lastname": "B"},
        }
        with (
            patch("app_auth.api.views.requests.post") as post,
            patch("app_auth.api.views.sync_bikes_from_strava"),
            patch("app_auth.api.views.send_welcome_email_task"),
        ):
            post.return_value.status_code = 200
            post.return_value.json.return_value = response_data
            response = self.client.post(AUTH_URL, {"code": "c", **extra}, format="json")
        self.assertEqual(response.status_code, 200)
        return StravaProfile.objects.get(strava_athlete_id=777)

    def test_checkbox_sets_weather_consent(self):
        self.assertIsNotNone(self._login(weather_consent=True).weather_consent_at)

    def test_without_checkbox_there_is_no_consent(self):
        self.assertIsNone(self._login().weather_consent_at)

    def test_relogin_without_checkbox_does_not_revoke(self):
        self._login(weather_consent=True)
        self.assertIsNotNone(self._login().weather_consent_at)


class WeatherConsentImportTests(ConsentTestCase):
    @patch("app_dashboard.api.services.StravaStreamService.fetch_activity_streams")
    @patch("app_dashboard.api.services.WeatherService.get_historical_weather")
    def _import(self, weather, streams):
        weather.return_value = None
        streams.return_value = None
        activity = {
            "id": 9001,
            "name": "Runde",
            "map": {},
            "start_date_local": "2026-05-01T08:00:00Z",
            "start_date": "2026-05-01T08:00:00Z",
            "start_latlng": [50.0, 8.0],
            "distance": 1000,
            "elapsed_time": 100,
            "gear_id": None,
        }
        StravaImportService.sync_activity_to_db(activity, self.profile)
        return weather

    def test_no_coordinates_leave_the_server_without_consent(self):
        self._import().assert_not_called()
        self.assertTrue(Ride.objects.filter(strava_id=9001).exists())

    def test_weather_is_fetched_with_consent(self):
        self.profile.weather_consent_at = timezone.now()
        self.profile.save(update_fields=["weather_consent_at"])
        self._import().assert_called_once()


class AiConsentGateTests(ConsentTestCase):
    def setUp(self):
        super().setUp()
        self.bike = Bike.objects.create(athlete=self.profile, name="Rad")

    def _ride(self, **fields):
        return Ride.objects.create(
            athlete=self.profile,
            name="Runde",
            distance=1000,
            start_date=timezone.now(),
            **fields,
        )

    def test_assistant_refuses_without_consent_and_never_calls_the_provider(self):
        with patch("app_maintenance.api.views.suggest_models") as suggest:
            response = self.client.post(
                "/api/maintenance/assistant/models/",
                {"manufacturer": "Canyon"},
                format="json",
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "ai_consent_required")
        suggest.assert_not_called()

    def test_assistant_runs_with_consent(self):
        StravaProfile.objects.filter(pk=self.profile.pk).update(
            ai_consent_at=timezone.now()
        )
        with patch("app_maintenance.api.views.suggest_models", return_value=[]):
            response = self.client.post(
                "/api/maintenance/assistant/models/",
                {"manufacturer": "Canyon"},
                format="json",
            )
        self.assertEqual(response.status_code, 200)

    def test_ride_summary_refuses_without_consent(self):
        ride = self._ride(strava_id=1)
        with patch("app_dashboard.api.views.get_ai_provider") as provider:
            response = self.client.get(f"/api/activities/{ride.id}/summary/")
        self.assertEqual(response.status_code, 403)
        provider.assert_not_called()

    def test_wear_impact_keeps_numbers_without_consent(self):
        ride = self._ride(strava_id=3)
        breakdown = {"component_count": 1}
        with (
            patch(
                "app_dashboard.api.views.ride_wear_breakdown", return_value=breakdown
            ),
            patch("app_dashboard.api.views.generate_reviewed_text") as generate,
        ):
            response = self.client.get(f"/api/activities/{ride.id}/wear-impact/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ai_consent_required"])
        self.assertEqual(response.json()["breakdown"], breakdown)
        generate.assert_not_called()

    def test_cached_ride_summary_stays_readable_without_consent(self):
        ride = self._ride(strava_id=2, ai_summary="Schon da.")
        response = self.client.get(f"/api/activities/{ride.id}/summary/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["summary"], "Schon da.")
