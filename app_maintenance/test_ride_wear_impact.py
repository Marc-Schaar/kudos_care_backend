"""
Tests fuer die fahrtbezogene Verschleiss-Aufschluesselung
(`app_maintenance/api/services.py::ride_wear_breakdown`) und den zugehoerigen
Endpoint `GET /api/activities/<id>/wear-impact/`.

Kernaussage der Suite: derselbe Bremsbelag kostet auf einer Regenfahrt mehr als auf
einer Trockenfahrt — die Rechnung war schon immer fahrtbezogen, sichtbar war sie nur nie.
"""

from datetime import date, timedelta
from unittest.mock import patch

import requests
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from app_auth.models import StravaProfile
from app_dashboard.models import Ride
from app_maintenance.api.services import (
    WeatherWearCalculator,
    ride_wear_breakdown,
    ride_wear_impact_is_stale,
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

DRY = {
    "precipitation": [0.0, 0.0],
    "temperature_2m": [18.0, 19.0],
    "wind_speed_10m": [8.0, 9.0],
}
RAINY = {
    "precipitation": [6.0, 7.0],
    "temperature_2m": [12.0, 13.0],
    "wind_speed_10m": [10.0, 11.0],
}


class RideWearBreakdownTests(APITestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(username="wear-impact")
        self.profile = StravaProfile.objects.create(
            user=user,
            strava_athlete_id=778899,
            access_token="t",
            refresh_token="r",
            expires_at=0,
        )
        self.bike = Bike.objects.create(
            athlete=self.profile,
            strava_bike_id="impact-bike",
            name="Impact Bike",
            bike_type=BikeType.GRAVEL,
        )
        WeatherSensitivityCoefficient.objects.update_or_create(
            category=ComponentCategory.BRAKES,
            defaults={
                "rain_sensitivity": 0.9,
                "heat_sensitivity": 0.0,
                "cold_sensitivity": 0.0,
                "wind_sensitivity": 0.0,
            },
        )
        template = ComponentTemplate.objects.create(
            name="Bremsbelag vorne",
            category=ComponentCategory.BRAKES,
            warn_km=1000,
        )
        self.slot = ComponentSlot.objects.create(bike=self.bike, template=template)
        self.component = Component.objects.create(
            slot=self.slot,
            installed_at=date.today() - timedelta(days=200),
            distance_at_install=0,
            is_mounted=True,
        )

    def _ride(self, weather, distance_m=20_000, days_ago=5, strava_id=1):
        return Ride.objects.create(
            strava_id=strava_id,
            name=f"Ride {strava_id}",
            distance=distance_m,
            start_date=timezone.now() - timedelta(days=days_ago),
            elapsed_time=3600,
            athlete=self.profile,
            bike=self.bike,
            weather_data=weather,
        )

    def test_dry_ride_costs_no_extra(self):
        breakdown = ride_wear_breakdown(self._ride(DRY, strava_id=1))
        brakes = breakdown["categories"][0]
        self.assertEqual(brakes["extra_pct"], 0)
        self.assertEqual(breakdown["total_extra_km"], 0.0)

    def test_rainy_ride_costs_extra_on_the_same_component(self):
        breakdown = ride_wear_breakdown(self._ride(RAINY, strava_id=2))
        brakes = breakdown["categories"][0]
        self.assertGreater(brakes["extra_pct"], 0)
        self.assertGreater(breakdown["total_extra_km"], 0.0)
        self.assertEqual(brakes["dominant_driver"], "rain")
        self.assertEqual(brakes["dominant_driver_display"], "Regen")

    def test_share_of_life_answers_what_the_ride_cost(self):
        """20 km bei 1000 km Lebensdauer = 2 % trocken, mehr bei Regen."""
        dry = ride_wear_breakdown(self._ride(DRY, strava_id=3))
        rainy = ride_wear_breakdown(self._ride(RAINY, strava_id=4))

        dry_share = dry["categories"][0]["components"][0]["share_of_life_pct"]
        rainy_share = rainy["categories"][0]["components"][0]["share_of_life_pct"]

        self.assertAlmostEqual(dry_share, 2.0, places=1)
        self.assertGreater(rainy_share, dry_share)

    def test_component_installed_after_the_ride_is_excluded(self):
        ride = self._ride(RAINY, days_ago=5, strava_id=5)
        self.component.installed_at = date.today() - timedelta(days=1)
        self.component.save()

        breakdown = ride_wear_breakdown(ride)
        self.assertEqual(breakdown["component_count"], 0)

    def test_component_retired_before_the_ride_is_excluded(self):
        ride = self._ride(RAINY, days_ago=5, strava_id=6)
        self.component.is_mounted = False
        self.component.retired_at = date.today() - timedelta(days=30)
        self.component.save()

        breakdown = ride_wear_breakdown(ride)
        self.assertEqual(breakdown["component_count"], 0)

    def test_component_retired_after_the_ride_is_still_counted(self):
        """
        Historie zaehlt: ein Teil, das zum Fahrtzeitpunkt montiert war, hat den
        Verschleiss abbekommen — auch wenn es heute nicht mehr am Rad ist.
        """
        ride = self._ride(RAINY, days_ago=20, strava_id=7)
        self.component.is_mounted = False
        self.component.retired_at = date.today() - timedelta(days=2)
        self.component.save()

        breakdown = ride_wear_breakdown(ride)
        self.assertEqual(breakdown["component_count"], 1)

    def test_never_mounted_spare_is_excluded(self):
        """Ersatzteil im Regal: nie montiert, nie ausgebaut — darf nicht mitzaehlen."""
        Component.objects.filter(pk=self.component.pk).update(is_mounted=False)
        ride = self._ride(RAINY, strava_id=8)

        breakdown = ride_wear_breakdown(ride)
        self.assertEqual(breakdown["component_count"], 0)

    def test_ride_without_bike_returns_empty(self):
        ride = self._ride(RAINY, strava_id=9)
        ride.bike = None
        ride.save()
        self.assertEqual(ride_wear_breakdown(ride)["component_count"], 0)


class RideMultiplierDetailTests(APITestCase):
    """Der Wrapper darf sich nicht anders verhalten als vorher."""

    def _coeff(self, **kwargs):
        defaults = {
            "rain_sensitivity": 0.0,
            "heat_sensitivity": 0.0,
            "cold_sensitivity": 0.0,
            "wind_sensitivity": 0.0,
        }
        return WeatherSensitivityCoefficient(
            category=ComponentCategory.DRIVETRAIN, **{**defaults, **kwargs}
        )

    def test_wrapper_matches_detail(self):
        coeff = self._coeff(rain_sensitivity=0.9)
        detail = WeatherWearCalculator.ride_multiplier_detail(RAINY, coeff)
        plain = WeatherWearCalculator.ride_multiplier(RAINY, coeff)
        self.assertEqual(plain, detail["multiplier"])

    def test_missing_data_has_no_dominant_driver(self):
        detail = WeatherWearCalculator.ride_multiplier_detail(
            {"precipitation": [1.0]}, self._coeff(rain_sensitivity=0.9)
        )
        self.assertEqual(detail["multiplier"], 1.0)
        self.assertIsNone(detail["dominant_driver"])

    def test_cold_ride_reports_cold_as_driver(self):
        coeff = self._coeff(cold_sensitivity=0.8, rain_sensitivity=0.1)
        cold = {
            "precipitation": [0.0],
            "temperature_2m": [-5.0],
            "wind_speed_10m": [5.0],
        }
        detail = WeatherWearCalculator.ride_multiplier_detail(cold, coeff)
        self.assertEqual(detail["dominant_driver"], "cold")


@override_settings(AI_PROVIDER="gemini", GEMINI_API_KEY="k", GROQ_API_KEY="")
class ActivityWearImpactViewTests(RideWearBreakdownTests):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.profile.user)
        session = self.client.session
        session["strava_athlete_id"] = self.profile.strava_athlete_id
        session.save()

    def _gemini_response(self, text):
        class _Resp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"candidates": [{"content": {"parts": [{"text": text}]}}]}

        return _Resp()

    @patch("app_maintenance.api.ai_providers.requests.post")
    def test_returns_breakdown_and_caches_summary(self, mock_post):
        mock_post.return_value = self._gemini_response("Diese Fahrt kostete etwas mehr.")
        ride = self._ride(RAINY, strava_id=20)

        response = self.client.get(f"/api/activities/{ride.id}/wear-impact/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["cached"])
        self.assertGreater(response.data["breakdown"]["categories"][0]["extra_pct"], 0)

        cached = self.client.get(f"/api/activities/{ride.id}/wear-impact/")
        self.assertTrue(cached.data["cached"])
        self.assertEqual(mock_post.call_count, 1)

    @patch("app_maintenance.api.ai_providers.requests.post")
    def test_numbers_survive_an_ai_outage(self, mock_post):
        """
        Der Erzaehltext ist die Zugabe, die Zahlen sind die Aussage — ein
        KI-Ausfall darf deshalb kein 503 ausloesen (anders als bei den reinen
        Erklaer-Endpoints).
        """
        mock_post.side_effect = requests.exceptions.Timeout("timed out")
        ride = self._ride(RAINY, strava_id=21)

        response = self.client.get(f"/api/activities/{ride.id}/wear-impact/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["ai_unavailable"])
        self.assertEqual(response.data["summary"], "")
        self.assertGreater(response.data["breakdown"]["component_count"], 0)

    @patch("app_maintenance.api.ai_providers.requests.post")
    def test_component_change_invalidates_cached_summary(self, mock_post):
        mock_post.return_value = self._gemini_response("Erste Fassung.")
        ride = self._ride(RAINY, strava_id=22)
        self.client.get(f"/api/activities/{ride.id}/wear-impact/")

        # Nutzer korrigiert nachtraeglich das Einbaudatum -> Aufschluesselung kann
        # sich geaendert haben, der Text muss neu erzaehlt werden.
        self.component.installed_at = date.today() - timedelta(days=300)
        self.component.save()

        mock_post.return_value = self._gemini_response("Zweite Fassung.")
        response = self.client.get(f"/api/activities/{ride.id}/wear-impact/")

        self.assertFalse(response.data["cached"])
        self.assertEqual(response.data["summary"], "Zweite Fassung.")

    def test_ride_without_components_returns_404(self):
        Component.objects.all().delete()
        ride = self._ride(RAINY, strava_id=23)

        response = self.client.get(f"/api/activities/{ride.id}/wear-impact/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data["code"], "no_data")

    def test_requires_authentication(self):
        ride = self._ride(RAINY, strava_id=24)
        self.client.logout()

        response = self.client.get(f"/api/activities/{ride.id}/wear-impact/")
        self.assertIn(
            response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )

    @patch("app_maintenance.api.ai_providers.requests.post")
    def test_stale_check_is_false_right_after_generation(self, mock_post):
        mock_post.return_value = self._gemini_response("Frisch.")
        ride = self._ride(RAINY, strava_id=25)
        self.client.get(f"/api/activities/{ride.id}/wear-impact/")

        ride.refresh_from_db()
        self.assertFalse(ride_wear_impact_is_stale(ride))
