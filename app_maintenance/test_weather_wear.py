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
from app_maintenance.api.tasks import recompute_weather_wear_for_bike
from app_maintenance.models import (
    AssemblyUsagePeriod,
    Bike,
    BikeAssembly,
    BikeType,
    Component,
    ComponentCategory,
    ComponentGroup,
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
        self.assertEqual(
            WeatherWearCalculator.ride_multiplier(weather_data, coeff), 1.0
        )

    def test_moderate_rain_scales_with_rain_sensitivity(self):
        coeff = self._coeff(rain=0.90)
        weather_data = {
            "precipitation": [2.0, 2.0],
            "temperature_2m": [15.0],
            "wind_speed_10m": [10.0],
        }
        # rain_factor = clamp(2.0/2.5, 0, 3.0) = 0.8 -> multiplier = 1 + 0.9*0.8 = 1.72
        self.assertAlmostEqual(
            WeatherWearCalculator.ride_multiplier(weather_data, coeff), 1.72, places=6
        )

    def test_extreme_rain_hits_multiplier_cap(self):
        coeff = self._coeff(rain=0.90)
        weather_data = {
            "precipitation": [10.0],
            "temperature_2m": [12.0],
            "wind_speed_10m": [5.0],
        }
        # rain_factor capped at 3.0 -> raw = 1 + 0.9*3.0 = 3.7 -> clamped to MAX_MULTIPLIER
        self.assertEqual(
            WeatherWearCalculator.ride_multiplier(weather_data, coeff), MAX_MULTIPLIER
        )

    def test_missing_arrays_yield_neutral_multiplier(self):
        coeff = self._coeff(rain=0.9, heat=0.9, cold=0.9, wind=0.9)
        self.assertEqual(WeatherWearCalculator.ride_multiplier({}, coeff), 1.0)
        self.assertEqual(WeatherWearCalculator.ride_multiplier(None, coeff), 1.0)

    def test_partial_data_is_treated_as_missing(self):
        coeff = self._coeff(rain=0.9)
        weather_data = {"precipitation": [5.0]}  # temperature_2m/wind_speed_10m fehlen
        self.assertEqual(
            WeatherWearCalculator.ride_multiplier(weather_data, coeff), 1.0
        )

    def test_cold_factor_activates_below_threshold(self):
        coeff = self._coeff(cold=0.45)
        weather_data = {
            "precipitation": [0.0],
            "temperature_2m": [-5.0],
            "wind_speed_10m": [5.0],
        }
        # cold_factor = clamp((5 - (-5))/15, 0, 2.0) = 0.6667 -> multiplier = 1 + 0.45*0.6667
        self.assertAlmostEqual(
            WeatherWearCalculator.ride_multiplier(weather_data, coeff), 1.3, places=2
        )

    def test_multiplier_never_below_one(self):
        coeff = self._coeff(rain=0.0, heat=0.0, cold=0.0, wind=0.0)
        weather_data = {
            "precipitation": [0.0],
            "temperature_2m": [20.0],
            "wind_speed_10m": [0.0],
        }
        self.assertEqual(
            WeatherWearCalculator.ride_multiplier(weather_data, coeff), 1.0
        )


class WeatherWearServiceTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="weather-athlete", password="pw"
        )
        self.profile = _make_profile(self.user)
        self.bike = Bike.objects.create(
            athlete=self.profile,
            strava_bike_id="wb1",
            name="Regenrad",
            bike_type=BikeType.ROAD,
        )
        self.template = ComponentTemplate.objects.create(
            name="Kette",
            category=ComponentCategory.DRIVETRAIN,
            warn_km=2000,
            is_system=False,
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
        weather_wear_km, ride_count = WeatherWearService.calculate_weather_wear_km(
            self.component
        )
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
        weather_wear_km, ride_count = WeatherWearService.calculate_weather_wear_km(
            self.component
        )
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
        weather_wear_km, ride_count = WeatherWearService.calculate_weather_wear_km(
            self.component
        )
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


class RecomputeWeatherWearForBikeTaskTests(APITestCase):
    """
    Deckt den in app_notifications hinzugefügten Hook ab: nach erfolgreicher
    Neuberechnung wird sofort geprüft, ob eine Warn-E-Mail fällig ist, statt erst beim
    nächsten täglichen Check (siehe app_maintenance/api/tasks.py).
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="hook-athlete", password="pw"
        )
        self.profile = _make_profile(self.user, strava_athlete_id=54322)
        self.bike = Bike.objects.create(
            athlete=self.profile,
            strava_bike_id="hook1",
            name="Hookrad",
            bike_type=BikeType.ROAD,
        )

    @patch("app_notifications.tasks.check_component_warnings_for_bike.delay")
    def test_enqueues_warning_check_after_successful_recompute(self, mock_delay):
        recompute_weather_wear_for_bike(self.bike.id)
        mock_delay.assert_called_once_with(self.bike.id)

    @patch("app_maintenance.api.services.WeatherWearService.recompute_bike")
    @patch("app_notifications.tasks.check_component_warnings_for_bike.delay")
    def test_does_not_enqueue_when_recompute_fails(
        self, mock_delay, mock_recompute_bike
    ):
        mock_recompute_bike.side_effect = Exception("boom")

        with self.assertRaises(Exception):
            recompute_weather_wear_for_bike(self.bike.id)

        mock_delay.assert_not_called()


def _gemini_response(
    text="Regen hat deine Kette stärker beansprucht als die reinen km zeigen.",
):
    resp = Mock()
    resp.raise_for_status = Mock()
    resp.json.return_value = {"candidates": [{"content": {"parts": [{"text": text}]}}]}
    return resp


def _groq_response(
    text="Regen hat deine Kette stärker beansprucht als die reinen km zeigen.",
):
    resp = Mock()
    resp.raise_for_status = Mock()
    resp.json.return_value = {"choices": [{"message": {"content": text}}]}
    return resp


def _make_post_side_effect(gemini_texts=None, groq_texts=None):
    """Gemini- und Groq-Aufrufe unterscheiden sich nur durch den Host in der URL —
    liefert der Reihe nach die naechste konfigurierte Antwort fuer den jeweiligen
    Provider, unabhaengig davon ob er gerade generiert oder eine Zweit-Pruefung macht.
    """
    gemini_iter = iter(gemini_texts or [])
    groq_iter = iter(groq_texts or [])

    def _side_effect(url, *args, **kwargs):
        if "generativelanguage.googleapis.com" in url:
            return _gemini_response(next(gemini_iter))
        return _groq_response(next(groq_iter))

    return _side_effect


@override_settings(AI_PROVIDER="gemini", GEMINI_API_KEY="test-key", GROQ_API_KEY="")
class ComponentWeatherExplanationViewTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="weather-explain", password="pw"
        )
        self.profile = _make_profile(self.user, strava_athlete_id=98765)
        self.client.force_login(self.user)
        session = self.client.session
        session["strava_athlete_id"] = self.profile.strava_athlete_id
        session.save()

        self.bike = Bike.objects.create(
            athlete=self.profile,
            strava_bike_id="wb2",
            name="Erklärrad",
            bike_type=BikeType.ROAD,
        )
        self.template = ComponentTemplate.objects.create(
            name="Kette",
            category=ComponentCategory.DRIVETRAIN,
            warn_km=2000,
            is_system=False,
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

    @override_settings(GROQ_API_KEY="test-groq-key")
    @patch("app_maintenance.api.ai_providers.requests.post")
    def test_provider_error_returns_503_after_gemini_and_groq_fallback_fail(
        self, mock_post
    ):
        self.component.weather_wear_km = 60.0
        self.component.weather_wear_ride_count = 3
        self.component.weather_wear_computed_at = timezone.now()
        self.component.save()
        mock_post.side_effect = requests.exceptions.Timeout("timed out")

        response = self.client.get(self._url())
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(response.data["code"], "ai_unavailable")
        # Gemini (Flash-Lite) und danach Groq wurden beide versucht.
        self.assertEqual(mock_post.call_count, 2)

    @override_settings(GEMINI_API_KEY="", GROQ_API_KEY="")
    @patch("app_maintenance.api.ai_providers.requests.post")
    def test_missing_api_keys_returns_503_without_http_call(self, mock_post):
        self.component.weather_wear_km = 60.0
        self.component.weather_wear_ride_count = 3
        self.component.weather_wear_computed_at = timezone.now()
        self.component.save()

        response = self.client.get(self._url())
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        mock_post.assert_not_called()

    @override_settings(GROQ_API_KEY="test-groq-key")
    @patch("app_maintenance.api.ai_providers.requests.post")
    def test_review_by_other_provider_passes_and_returns_explanation(self, mock_post):
        self.component.weather_wear_km = 60.0
        self.component.weather_wear_ride_count = 3
        self.component.weather_wear_computed_at = timezone.now()
        self.component.save()
        mock_post.side_effect = _make_post_side_effect(
            gemini_texts=[
                "Regen hat deine Kette stärker beansprucht als die reinen km zeigen."
            ],
            groq_texts=["OK"],
        )

        response = self.client.get(self._url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("Regen", response.data["explanation"])
        # Gemini generiert, Groq prueft gegen (2 Aufrufe, keine Neugenerierung noetig).
        self.assertEqual(mock_post.call_count, 2)

    @override_settings(GROQ_API_KEY="test-groq-key")
    @patch("app_maintenance.api.ai_providers.requests.post")
    def test_failed_review_triggers_one_regeneration(self, mock_post):
        self.component.weather_wear_km = 60.0
        self.component.weather_wear_ride_count = 3
        self.component.weather_wear_computed_at = timezone.now()
        self.component.save()
        mock_post.side_effect = _make_post_side_effect(
            gemini_texts=["Erste, fragwürdige Antwort.", "Zweite, bessere Antwort."],
            groq_texts=["FEHLER: widerspricht den angegebenen Zahlen"],
        )

        response = self.client.get(self._url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("Zweite", response.data["explanation"])
        # Generieren, Zweit-Pruefung (durchgefallen), einmalige Neugenerierung.
        self.assertEqual(mock_post.call_count, 3)

    @override_settings(GEMINI_API_KEY="", GROQ_API_KEY="test-groq-key")
    @patch("app_maintenance.api.ai_providers.requests.post")
    def test_gemini_failure_falls_back_to_groq(self, mock_post):
        self.component.weather_wear_km = 60.0
        self.component.weather_wear_ride_count = 3
        self.component.weather_wear_computed_at = timezone.now()
        self.component.save()
        mock_post.return_value = _groq_response()

        response = self.client.get(self._url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("Regen", response.data["explanation"])
        # Gemini hatte keinen Key (kein HTTP-Call), Groq wurde genau 1x aufgerufen.
        mock_post.assert_called_once()


@override_settings(AI_PROVIDER="gemini", GEMINI_API_KEY="test-key", GROQ_API_KEY="")
class ComponentCheckInstructionsViewTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="check-instructions", password="pw"
        )
        self.profile = _make_profile(self.user, strava_athlete_id=98766)
        self.client.force_login(self.user)
        session = self.client.session
        session["strava_athlete_id"] = self.profile.strava_athlete_id
        session.save()

        self.bike = Bike.objects.create(
            athlete=self.profile,
            strava_bike_id="wb3",
            name="Anleitungsrad",
            bike_type=BikeType.ROAD,
        )
        # warn_days=10 sorgt dafür, dass die 30 Tage alte Komponente initial "critical" ist —
        # damit lässt sich die Status-Änderung nach einer Freigabe testen.
        self.template = ComponentTemplate.objects.create(
            name="Kette",
            category=ComponentCategory.DRIVETRAIN,
            warn_days=10,
            is_system=False,
        )
        self.slot = ComponentSlot.objects.create(bike=self.bike, template=self.template)
        self.component = Component.objects.create(
            slot=self.slot,
            brand="KMC",
            installed_at=date.today() - timedelta(days=30),
            is_mounted=True,
        )

    def _url(self, refresh=False):
        url = f"/api/maintenance/components/{self.component.id}/check-instructions/"
        return url + "?refresh=true" if refresh else url

    @patch("app_maintenance.api.ai_providers.requests.post")
    def test_happy_path_generates_and_caches_instructions(self, mock_post):
        mock_post.return_value = _gemini_response(
            "- Prüfe die Kette auf Längung mit einer Lehre."
        )

        response = self.client.get(self._url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["cached"])
        self.assertIn("Kette", response.data["instructions"])
        mock_post.assert_called_once()

        # Zweiter Aufruf: aus DB-Cache, kein erneuter Provider-Call
        response2 = self.client.get(self._url())
        self.assertEqual(response2.status_code, status.HTTP_200_OK)
        self.assertTrue(response2.data["cached"])
        mock_post.assert_called_once()

    @patch("app_maintenance.api.ai_providers.requests.post")
    def test_status_change_invalidates_cached_instructions(self, mock_post):
        mock_post.return_value = _gemini_response()
        self.client.get(self._url())
        mock_post.assert_called_once()

        # Freigabe setzt den Status von "critical" auf "ok" zurück -> Cache ungültig.
        response = self.client.post(
            f"/api/maintenance/components/{self.component.id}/check/", data={}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response2 = self.client.get(self._url())
        self.assertFalse(response2.data["cached"])
        self.assertEqual(mock_post.call_count, 2)

    @patch("app_maintenance.api.ai_providers.requests.post")
    def test_refresh_forces_regeneration(self, mock_post):
        mock_post.return_value = _gemini_response()
        self.client.get(self._url())
        mock_post.assert_called_once()

        response = self.client.get(self._url(refresh=True))
        self.assertFalse(response.data["cached"])
        self.assertEqual(mock_post.call_count, 2)

    @override_settings(GEMINI_API_KEY="", GROQ_API_KEY="")
    @patch("app_maintenance.api.ai_providers.requests.post")
    def test_missing_api_keys_returns_503_without_http_call(self, mock_post):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        mock_post.assert_not_called()

    def test_requires_authentication(self):
        self.client.logout()
        response = self.client.get(self._url())
        self.assertIn(
            response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )


@override_settings(AI_PROVIDER="gemini", GEMINI_API_KEY="test-key", GROQ_API_KEY="")
class BikeConditionReportViewTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="condition-report", password="pw"
        )
        self.profile = _make_profile(self.user, strava_athlete_id=11111)
        self.client.force_login(self.user)
        session = self.client.session
        session["strava_athlete_id"] = self.profile.strava_athlete_id
        session.save()

        self.bike = Bike.objects.create(
            athlete=self.profile,
            strava_bike_id="cr1",
            name="Berichtrad",
            bike_type=BikeType.ROAD,
        )
        self.template = ComponentTemplate.objects.create(
            name="Kette",
            category=ComponentCategory.DRIVETRAIN,
            warn_km=2000,
            is_system=False,
        )
        self.slot = ComponentSlot.objects.create(bike=self.bike, template=self.template)
        self.component = Component.objects.create(
            slot=self.slot,
            brand="KMC",
            installed_at=date.today() - timedelta(days=30),
            distance_at_install=0,
            is_mounted=True,
        )

    def _url(self, refresh=False):
        url = f"/api/maintenance/bikes/{self.bike.id}/condition-report/"
        return url + "?refresh=true" if refresh else url

    def test_no_mounted_components_returns_404(self):
        self.component.is_mounted = False
        self.component.save()
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data["code"], "no_data")

    @patch("app_maintenance.api.ai_providers.requests.post")
    def test_happy_path_generates_and_caches_report(self, mock_post):
        mock_post.return_value = _gemini_response(
            "Die Kette ist unauffällig, keine Komponente braucht aktuell Aufmerksamkeit."
        )

        response = self.client.get(self._url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["cached"])
        self.assertIn("Kette", response.data["report"])
        mock_post.assert_called_once()

        # Zweiter Aufruf: aus DB-Cache, kein erneuter Provider-Call
        response2 = self.client.get(self._url())
        self.assertEqual(response2.status_code, status.HTTP_200_OK)
        self.assertTrue(response2.data["cached"])
        mock_post.assert_called_once()

    @patch("app_maintenance.api.ai_providers.requests.post")
    def test_new_ride_invalidates_cached_report(self, mock_post):
        mock_post.return_value = _gemini_response()
        self.client.get(self._url())
        mock_post.assert_called_once()

        Ride.objects.create(
            strava_id=9001,
            name="Neue Fahrt",
            distance=10000,
            start_date=timezone.now(),
            athlete=self.profile,
            bike=self.bike,
        )

        response = self.client.get(self._url())
        self.assertFalse(response.data["cached"])
        self.assertEqual(mock_post.call_count, 2)

    def test_requires_authentication(self):
        self.client.logout()
        response = self.client.get(self._url())
        self.assertIn(
            response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )


class WeatherWearParkedAssemblyTests(APITestCase):
    """
    Fahrten, die in eine Parkphase der Baugruppe fallen, duerfen den
    Wetter-Verschleiss nicht erhoehen â€” der Laufradsatz lag im Keller.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="parked-athlete", password="pw"
        )
        self.profile = _make_profile(self.user, strava_athlete_id=778899)
        self.bike = Bike.objects.create(
            athlete=self.profile,
            strava_bike_id="pb1",
            name="Winterrad",
            bike_type=BikeType.ROAD,
        )
        self.group = ComponentGroup.objects.create(
            name="Laufrad vorne", category=ComponentCategory.WHEELS
        )
        self.template = ComponentTemplate.objects.create(
            name="Reifen vorne",
            category=ComponentCategory.WHEELS,
            warn_km=5000,
            is_system=False,
            group=self.group,
        )
        self.assembly = BikeAssembly.objects.create(
            bike=self.bike,
            group=self.group,
            installed_at=date.today() - timedelta(days=90),
        )
        self.slot = ComponentSlot.objects.create(
            bike=self.bike, assembly=self.assembly, template=self.template
        )
        self.component = Component.objects.create(
            slot=self.slot,
            installed_at=date.today() - timedelta(days=90),
            distance_at_install=0.0,
            is_mounted=True,
        )

    def _ride(self, strava_id, km, days_ago):
        return Ride.objects.create(
            strava_id=strava_id,
            name=f"Fahrt {strava_id}",
            distance=km * 1000,
            start_date=timezone.now() - timedelta(days=days_ago),
            athlete=self.profile,
            bike=self.bike,
            weather_data=None,
        )

    def test_rides_during_the_parked_gap_are_excluded(self):
        # Aufgezogen Tag -90 bis -60, dann geparkt, seit Tag -10 wieder drauf.
        AssemblyUsagePeriod.objects.create(
            assembly=self.assembly,
            started_at=date.today() - timedelta(days=90),
            started_distance_km=0.0,
            ended_at=date.today() - timedelta(days=60),
            ended_distance_km=40.0,
        )
        AssemblyUsagePeriod.objects.create(
            assembly=self.assembly,
            started_at=date.today() - timedelta(days=10),
            started_distance_km=90.0,
        )

        self._ride(2001, 40, days_ago=70)  # innerhalb der ersten Periode
        self._ride(2002, 50, days_ago=30)  # waehrend der Parkphase
        self._ride(2003, 25, days_ago=5)  # innerhalb der zweiten Periode

        weather_wear_km, ride_count = WeatherWearService.calculate_weather_wear_km(
            self.component
        )
        self.assertEqual(ride_count, 2, "Die Fahrt aus der Parkphase zaehlt nicht mit.")
        self.assertEqual(weather_wear_km, 65.0)

    def test_without_periods_all_rides_since_install_still_count(self):
        """Altbestand ohne Nutzungszeitraeume rechnet unveraendert weiter."""
        self._ride(2101, 40, days_ago=70)
        self._ride(2102, 50, days_ago=30)

        weather_wear_km, ride_count = WeatherWearService.calculate_weather_wear_km(
            self.component
        )
        self.assertEqual(ride_count, 2)
        self.assertEqual(weather_wear_km, 90.0)

    def test_recompute_bike_skips_parked_assemblies(self):
        AssemblyUsagePeriod.objects.create(
            assembly=self.assembly,
            started_at=date.today() - timedelta(days=90),
            started_distance_km=0.0,
            ended_at=date.today() - timedelta(days=60),
            ended_distance_km=40.0,
        )
        self.assembly.is_active = False
        self.assembly.save()

        self.assertEqual(WeatherWearService.recompute_bike(self.bike), 0)
        self.assertEqual(
            WeatherWearService.recompute_bike(self.bike, include_parked=True), 1
        )
