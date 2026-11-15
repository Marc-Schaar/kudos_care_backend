"""
Kurzer Wetterbericht für den voraussichtlichen Fahrtag (Dashboard-Prognose und
Vorhersage-Mail). Open-Meteo ist gemockt; geprüft wird, was aus den Stundenwerten wird,
dass ohne Einwilligung nichts das Haus verlässt, und dass der Verschleiß-Satz aus
derselben Formel kommt wie bei vergangenen Fahrten.
"""

from datetime import date, timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.gis.geos import Point
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from app_auth.models import StravaProfile
from app_dashboard.api import forecast_weather
from app_dashboard.api.forecast_weather import (
    compass_label,
    condition_label,
    summarize_hours,
)
from app_dashboard.models import Ride
from app_notifications.presentation import weather_note

from .api.services import ride_day_weather, weather_wear_note
from .models import (
    Bike,
    BikeType,
    Component,
    ComponentCategory,
    ComponentSlot,
    ComponentTemplate,
    WeatherSensitivityCoefficient,
)


def _hourly(day: date, *, precip: float = 0.0, temp: float = 15.0, wind: float = 10.0):
    times = [f"{day.isoformat()}T{h:02d}:00" for h in range(24)]
    return {
        "time": times,
        # Nachts deutlich kälter und mit Gewitter: darf das Fahrfenster nicht färben.
        "temperature_2m": [temp if 8 <= h < 20 else -5.0 for h in range(24)],
        "precipitation": [precip if 8 <= h < 20 else 9.0 for h in range(24)],
        "precipitation_probability": [70 if 8 <= h < 20 else 100 for h in range(24)],
        "wind_speed_10m": [wind] * 24,
        "wind_gusts_10m": [wind * 2] * 24,
        "wind_direction_10m": [270] * 24,
        "weather_code": [61 if 8 <= h < 20 else 95 for h in range(24)],
    }


class SummarizeHoursTests(SimpleTestCase):
    def test_only_the_riding_window_counts(self) -> None:
        day = date(2026, 11, 19)
        summary = summarize_hours(_hourly(day, precip=0.5, temp=8.0), day)
        self.assertEqual(summary["temp_min"], 8.0)
        self.assertEqual(summary["precipitation_mm"], 6.0)  # 12 h × 0,5
        self.assertEqual(summary["precipitation_probability"], 70)
        self.assertEqual(summary["condition"], "Regen")  # nicht das Nacht-Gewitter
        self.assertEqual(summary["wind_from"], "West")
        self.assertEqual(len(summary["hourly"]["temperature_2m"]), 12)

    def test_missing_columns_give_no_summary(self) -> None:
        day = date(2026, 11, 19)
        hourly = _hourly(day)
        hourly["wind_speed_10m"] = [None] * 24
        self.assertIsNone(summarize_hours(hourly, day))

    def test_labels(self) -> None:
        self.assertEqual(condition_label(0), "Klar")
        self.assertEqual(condition_label(63), "Regen")
        self.assertEqual(condition_label(96), "Gewitter")
        self.assertEqual(compass_label(350), "Nord")
        self.assertEqual(compass_label(225), "Südwest")


class RideDayWeatherTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        user = get_user_model().objects.create_user(username="w", password="pw")
        self.profile = StravaProfile.objects.create(
            user=user,
            strava_athlete_id=82001,
            access_token="t",
            refresh_token="r",
            expires_at=0,
            weather_consent_at=timezone.now(),
        )
        self.bike = Bike.objects.create(
            athlete=self.profile,
            strava_bike_id="w1",
            name="Gravel",
            bike_type=BikeType.GRAVEL,
        )
        Ride.objects.create(
            strava_id=96001,
            name="Fahrt",
            distance=20000,
            start_date=timezone.now() - timedelta(days=2),
            athlete=self.profile,
            bike=self.bike,
            start_latlng=Point(13.40512, 52.52003, srid=4326),
        )
        template = ComponentTemplate.objects.create(
            name="Kette", category=ComponentCategory.DRIVETRAIN, warn_km=3000
        )
        slot = ComponentSlot.objects.create(bike=self.bike, template=template)
        Component.objects.create(slot=slot, brand="Test", installed_at=date.today())
        WeatherSensitivityCoefficient.objects.update_or_create(
            category=ComponentCategory.DRIVETRAIN,
            defaults={
                "rain_sensitivity": 0.5,
                "heat_sensitivity": 0.1,
                "cold_sensitivity": 0.1,
                "wind_sensitivity": 0.0,
            },
        )
        self.day = date.today() + timedelta(days=2)

    def _response(self, **kwargs):
        res = mock.Mock()
        res.json.return_value = {"hourly": _hourly(self.day, **kwargs)}
        res.raise_for_status.return_value = None
        return res

    def test_without_consent_nothing_is_sent(self) -> None:
        self.profile.weather_consent_at = None
        self.profile.save()
        self.bike.refresh_from_db()
        with mock.patch.object(forecast_weather.requests, "get") as get:
            result = ride_day_weather(self.bike, self.day)
        self.assertEqual(result, {"status": "consent_required", "report": None})
        get.assert_not_called()

    def test_coordinates_are_rounded_before_they_leave(self) -> None:
        with mock.patch.object(
            forecast_weather.requests, "get", return_value=self._response()
        ) as get:
            ride_day_weather(self.bike, self.day)
        params = get.call_args.kwargs["params"]
        self.assertEqual((params["latitude"], params["longitude"]), (52.52, 13.41))

    def test_rainy_day_names_the_drivetrain_and_the_chain(self) -> None:
        with mock.patch.object(
            forecast_weather.requests, "get", return_value=self._response(precip=2.5)
        ):
            result = ride_day_weather(self.bike, self.day)
        self.assertEqual(result["status"], "ok")
        wear = result["report"]["wear"]
        self.assertEqual(wear["driver"], "rain")
        self.assertEqual(wear["extra_pct"], 50)
        self.assertIn("der Antrieb", wear["text"])
        self.assertIn("Kette", wear["text"])
        self.assertNotIn("hourly", result["report"])

    def test_dry_day_says_no_surcharge(self) -> None:
        with mock.patch.object(
            forecast_weather.requests, "get", return_value=self._response()
        ):
            result = ride_day_weather(self.bike, self.day)
        self.assertIsNone(result["report"]["wear"]["driver"])
        self.assertIn("Kein Wetteraufschlag", result["report"]["wear"]["text"])

    def test_beyond_the_horizon_is_not_fetched(self) -> None:
        with mock.patch.object(forecast_weather.requests, "get") as get:
            result = ride_day_weather(self.bike, date.today() + timedelta(days=30))
        self.assertEqual(result["status"], "out_of_range")
        get.assert_not_called()

    def test_without_a_start_point_there_is_no_location(self) -> None:
        Ride.objects.update(start_latlng=None)
        result = ride_day_weather(self.bike, self.day)
        self.assertEqual(result["status"], "no_location")

    def test_open_meteo_failure_is_unavailable_and_not_cached(self) -> None:
        import requests

        with mock.patch.object(
            forecast_weather.requests,
            "get",
            side_effect=requests.ConnectionError(),
        ):
            result = ride_day_weather(self.bike, self.day)
        self.assertEqual(result["status"], "unavailable")
        with mock.patch.object(
            forecast_weather.requests, "get", return_value=self._response()
        ):
            self.assertEqual(ride_day_weather(self.bike, self.day)["status"], "ok")

    def test_plural_categories_get_a_plural_verb(self) -> None:
        WeatherSensitivityCoefficient.objects.update_or_create(
            category=ComponentCategory.BRAKES,
            defaults={
                "rain_sensitivity": 0.9,
                "heat_sensitivity": 0.0,
                "cold_sensitivity": 0.0,
                "wind_sensitivity": 0.0,
            },
        )
        hourly = {
            "temperature_2m": [12.0],
            "precipitation": [2.5],
            "wind_speed_10m": [5],
        }
        note = weather_wear_note(hourly, [ComponentCategory.BRAKES])
        self.assertTrue(note["text"].startswith("Bei Nässe verschleißen die Bremsen"))


class WeatherNoteMailTests(SimpleTestCase):
    def test_mail_wording(self) -> None:
        note = weather_note(
            {
                "condition": "Regen",
                "temp_min": 4.2,
                "temp_max": 9.6,
                "precipitation_mm": 3.4,
                "precipitation_probability": 70,
                "wind_kmh": 24.0,
                "gusts_kmh": 46.0,
                "wind_from": "West",
                "wear": {"driver": "rain", "text": "Bei Nässe ..."},
            }
        )
        self.assertEqual(note.headline, "Regen, 4 bis 10 °C")
        self.assertEqual(
            note.facts,
            ["70 % Regenrisiko, 3,4 mm", "Wind aus West, 24 km/h, Böen 46"],
        )
        self.assertEqual(note.tone, "warn")

    def test_no_report_no_note(self) -> None:
        self.assertIsNone(weather_note(None))
