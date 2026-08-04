from datetime import date, timedelta
from unittest.mock import Mock, patch

import requests
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from app_auth.models import StravaProfile
from app_dashboard.models import Ride
from app_maintenance.api.services import (
    MAX_MULTIPLIER,
    WeatherWearCalculator,
    WeatherWearService,
)
from app_maintenance.models import (
    Bike,
    BikeType,
    Component,
    ComponentCategory,
    ComponentSlot,
    ComponentTemplate,
    WeatherSensitivityCoefficient,
)


def _make_profile(user, strava_athlete_id=54321):
    return StravaProfile.objects.create(
        user=user,
        strava_athlete_id=strava_athlete_id,
        access_token="token",
        refresh_token="refresh",
        expires_at=0,
    )


class WeatherWearCalculatorTests(SimpleTestCase):
    """Reine Formel-Tests — keine DB nötig (Coefficient-Instanz wird nicht gespeichert)."""

    def _coeff(self, rain=0.0, heat=0.0, cold=0.0, wind=0.0):
        return WeatherSensitivityCoefficient(
            category=ComponentCategory.DRIVETRAIN,
            rain_sensitivity=rain,
            heat_sensitivity=heat,
            cold_sensitivity=cold,
            wind_sensitivity=wind,
        )

    def test_mild_conditions_yield_neutral_multiplier(self):
        coeff = self._coeff(rain=0.9, heat=0.5, cold=0.5, wind=0.1)
        weather_data = {
            "precipitation": [0.0, 0.0],
            "temperature_2m": [18.0, 19.0],
            "wind_speed_10m": [8.0, 9.0],
        }
        self.assertEqual(WeatherWearCalculator.ride_multiplier(weather_data, coeff), 1.0)

    def test_moderate_rain_scales_with_rain_sensitivity(self):
        coeff = self._coeff(rain=0.90)
        weather_data = {
            "precipitation": [2.0, 2.0],
            "temperature_2m": [15.0],
            "wind_speed_10m": [10.0],
        }
        # rain_factor = clamp(2.0/2.5, 0, 3.0) = 0.8 -> multiplier = 1 + 0.9*0.8 = 1.72
        self.assertAlmostEqual(WeatherWearCalculator.ride_multiplier(weather_data, coeff), 1.72, places=6)

    def test_extreme_rain_hits_multiplier_cap(self):
        coeff = self._coeff(rain=0.90)
        weather_data = {
            "precipitation": [10.0],
            "temperature_2m": [12.0],
            "wind_speed_10m": [5.0],
        }
        # rain_factor capped at 3.0 -> raw = 1 + 0.9*3.0 = 3.7 -> clamped to MAX_MULTIPLIER
        self.assertEqual(WeatherWearCalculator.ride_multiplier(weather_data, coeff), MAX_MULTIPLIER)

    def test_missing_arrays_yield_neutral_multiplier(self):
        coeff = self._coeff(rain=0.9, heat=0.9, cold=0.9, wind=0.9)
        self.assertEqual(WeatherWearCalculator.ride_multiplier({}, coeff), 1.0)
        self.assertEqual(WeatherWearCalculator.ride_multiplier(None, coeff), 1.0)

    def test_partial_data_is_treated_as_missing(self):
        coeff = self._coeff(rain=0.9)
        weather_data = {"precipitation": [5.0]}  # temperature_2m/wind_speed_10m fehlen
        self.assertEqual(WeatherWearCalculator.ride_multiplier(weather_data, coeff), 1.0)

    def test_cold_factor_activates_below_threshold(self):
        coeff = self._coeff(cold=0.45)
        weather_data = {
            "precipitation": [0.0],
            "temperature_2m": [-5.0],
            "wind_speed_10m": [5.0],
        }
        # cold_factor = clamp((5 - (-5))/15, 0, 2.0) = 0.6667 -> multiplier = 1 + 0.45*0.6667
        self.assertAlmostEqual(WeatherWearCalculator.ride_multiplier(weather_data, coeff), 1.3, places=2)

    def test_multiplier_never_below_one(self):
        coeff = self._coeff(rain=0.0, heat=0.0, cold=0.0, wind=0.0)
        weather_data = {
            "precipitation": [0.0],
            "temperature_2m": [20.0],
            "wind_speed_10m": [0.0],
        }
        self.assertEqual(WeatherWearCalculator.ride_multiplier(weather_data, coeff), 1.0)


class WeatherWearServiceTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="weather-athlete", password="pw")
        self.profile = _make_profile(self.user)
        self.bike = Bike.objects.create(
            athlete=self.profile, strava_bike_id="wb1", name="Regenrad", bike_type=BikeType.ROAD
        )
        self.template = ComponentTemplate.objects.create(
            name="Kette", category=ComponentCategory.DRIVETRAIN, warn_km=2000, is_system=False
        )
        self.slot = ComponentSlot.objects.create(bike=self.bike, template=self.template)
        self.component = Component.objects.create(
            slot=self.slot,
            brand="KMC",
            installed_at=date.today() - timedelta(days=30),
            is_mounted=True,
        )

    def test_rides_without_weather_data_fall_back_to_raw_distance(self):
        Ride.objects.create(
            strava_id=1001,
            name="Trockene Fahrt",
            distance=20000,  # 20km
            start_date=timezone.now() - timedelta(days=5),
            athlete=self.profile,
            bike=self.bike,
            weather_data=None,
        )
        weather_wear_km, ride_count = WeatherWearService.calculate_weather_wear_km(self.component)
        self.assertEqual(ride_count, 1)
        self.assertEqual(weather_wear_km, 20.0)

    def test_rides_before_install_date_are_excluded(self):
        Ride.objects.create(
            strava_id=1002,
            name="Vor Einbau",
            distance=15000,
            start_date=timezone.now() - timedelta(days=60),
            athlete=self.profile,
            bike=self.bike,
        )
        weather_wear_km, ride_count = WeatherWearService.calculate_weather_wear_km(self.component)
        self.assertEqual(ride_count, 0)
        self.assertEqual(weather_wear_km, 0.0)

    def test_rainy_ride_produces_higher_effective_km_than_raw(self):
        Ride.objects.create(
            strava_id=1003,
            name="Regenfahrt",
            distance=50000,  # 50km
            start_date=timezone.now() - timedelta(days=2),
            athlete=self.profile,
            bike=self.bike,
            weather_data={
                "precipitation": [5.0, 6.0],
                "temperature_2m": [12.0, 12.0],
                "wind_speed_10m": [5.0, 5.0],
            },
        )
        weather_wear_km, ride_count = WeatherWearService.calculate_weather_wear_km(self.component)
        self.assertEqual(ride_count, 1)
        self.assertGreater(weather_wear_km, 50.0)

    def test_recompute_component_persists_all_three_fields(self):
        Ride.objects.create(
            strava_id=1004,
            name="Fahrt",
            distance=30000,
            start_date=timezone.now() - timedelta(days=1),
            athlete=self.profile,
            bike=self.bike,
        )
        WeatherWearService.recompute_component(self.component)
        self.component.refresh_from_db()
        self.assertEqual(self.component.weather_wear_km, 30.0)
        self.assertEqual(self.component.weather_wear_ride_count, 1)
        self.assertIsNotNone(self.component.weather_wear_computed_at)


def _gemini_response(text="Regen hat deine Kette stärker beansprucht als die reinen km zeigen."):
    resp = Mock()
    resp.raise_for_status = Mock()
    resp.json.return_value = {"candidates": [{"content": {"parts": [{"text": text}]}}]}
    return resp


@override_settings(AI_PROVIDER="gemini", GEMINI_API_KEY="test-key")
class ComponentWeatherExplanationViewTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="weather-explain", password="pw")
        self.profile = _make_profile(self.user, strava_athlete_id=98765)
        self.client.force_login(self.user)
        session = self.client.session
        session["strava_athlete_id"] = self.profile.strava_athlete_id
        session.save()

        self.bike = Bike.objects.create(
            athlete=self.profile, strava_bike_id="wb2", name="Erklärrad", bike_type=BikeType.ROAD
        )
        self.template = ComponentTemplate.objects.create(
            name="Kette", category=ComponentCategory.DRIVETRAIN, warn_km=2000, is_system=False
        )
        self.slot = ComponentSlot.objects.create(bike=self.bike, template=self.template)
        self.component = Component.objects.create(
            slot=self.slot,
            brand="KMC",
            installed_at=date.today() - timedelta(days=30),
            is_mounted=True,
        )

    def _url(self, refresh=False):
        url = f"/api/maintenance/components/{self.component.id}/weather-explanation/"
        return url + "?refresh=true" if refresh else url

    def test_no_ride_data_returns_404(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data["code"], "no_data")

    @patch("app_maintenance.api.ai_providers.requests.post")
    def test_happy_path_generates_and_caches_explanation(self, mock_post):
        self.component.weather_wear_km = 60.0
        self.component.weather_wear_ride_count = 3
        self.component.weather_wear_computed_at = timezone.now()
        self.component.save()
        mock_post.return_value = _gemini_response()

        response = self.client.get(self._url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["cached"])
        self.assertIn("Regen", response.data["explanation"])
        mock_post.assert_called_once()

        # Zweiter Aufruf: aus DB-Cache, kein erneuter Provider-Call
        response2 = self.client.get(self._url())
        self.assertEqual(response2.status_code, status.HTTP_200_OK)
        self.assertTrue(response2.data["cached"])
        mock_post.assert_called_once()  # weiterhin nur 1x aufgerufen

    @patch("app_maintenance.api.ai_providers.requests.post")
    def test_provider_error_returns_503(self, mock_post):
        self.component.weather_wear_km = 60.0
        self.component.weather_wear_ride_count = 3
        self.component.weather_wear_computed_at = timezone.now()
        self.component.save()
        mock_post.side_effect = requests.exceptions.Timeout("timed out")

        response = self.client.get(self._url())
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(response.data["code"], "ai_unavailable")

    @override_settings(GEMINI_API_KEY="")
    @patch("app_maintenance.api.ai_providers.requests.post")
    def test_missing_api_key_returns_503_without_http_call(self, mock_post):
        self.component.weather_wear_km = 60.0
        self.component.weather_wear_ride_count = 3
        self.component.weather_wear_computed_at = timezone.now()
        self.component.save()

        response = self.client.get(self._url())
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        mock_post.assert_not_called()
