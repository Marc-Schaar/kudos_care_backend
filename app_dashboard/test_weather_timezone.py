"""
Regressionstests für den Zeitzonen-Versatz beim Wetter einer Fahrt.

`Ride.start_date` ist Stravas `start_date_local` — Ortszeit mit angehängtem `Z`. Open-Meteo
lieferte ohne `timezone` UTC-Stunden, das Fenster jeder Fahrt lag also um den UTC-Abstand
daneben. Sichtbar wurde das an einer Regenfahrt, die in der Liste trocken erschien: der
Schauer lag eine Stunde vor dem Fenster, das die App für die Fahrt hielt.
"""

from datetime import datetime
from datetime import timezone as dt_timezone
from io import StringIO
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.contrib.gis.geos import Point
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from app_auth.models import StravaProfile
from app_dashboard.api.services import StravaImportService, WeatherService
from app_dashboard.models import Ride


def _local_hourly(day: str, rain_at: dict[int, float]) -> dict:
    """Open-Meteo-Antwort mit `timezone=auto`: Ortszeit ohne Zonenangabe."""
    hours = range(24)
    return {
        "hourly": {
            "time": [f"{day}T{h:02d}:00" for h in hours],
            "temperature_2m": [8.0 for _ in hours],
            "precipitation": [rain_at.get(h, 0.0) for h in hours],
            "wind_speed_10m": [10.0 for _ in hours],
            "wind_direction_10m": [270.0 for _ in hours],
        }
    }


class WeatherServiceTimezoneTests(TestCase):
    @patch("app_dashboard.api.services.requests.get")
    def test_asks_open_meteo_for_local_time(self, mock_get):
        mock_get.return_value = Mock(status_code=200, json=lambda: {"hourly": {}})
        mock_get.return_value.raise_for_status = Mock()

        WeatherService.get_historical_weather(50.1, 8.7, "2026-11-17")

        params = mock_get.call_args.kwargs["params"]
        self.assertEqual(params["timezone"], "auto")


class RideWeatherWindowTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(username="wetter", password="pw")
        self.profile = StravaProfile.objects.create(
            user=user,
            strava_athlete_id=777,
            access_token="t",
            refresh_token="r",
            expires_at=0,
            weather_consent_at=timezone.now(),
        )

    @patch("app_dashboard.api.services.recompute_weather_wear_for_bike.delay")
    @patch("app_dashboard.api.services.StravaStreamService.fetch_activity_streams")
    @patch("app_dashboard.api.services.WeatherService.get_ride_rain", return_value=None)
    @patch("app_dashboard.api.services.WeatherService.get_historical_weather")
    def test_rain_during_the_ride_lands_in_its_window(
        self, mock_weather, _ride_rain, mock_streams, _delay
    ):
        # Fahrt 16:10–17:00 Ortszeit; geregnet hat es 16–17 Uhr (Wert um 17:00).
        # Mit UTC-Stunden stünde der Schauer bei 16:00 und das Fenster bei 17–18 Uhr.
        mock_weather.return_value = _local_hourly("2026-11-17", {17: 1.4})
        mock_streams.return_value = None

        ride = StravaImportService.sync_activity_to_db(
            {
                "id": 9001,
                "name": "Regenrunde",
                "map": {},
                "start_date_local": "2026-11-17T16:10:00Z",
                "start_date": "2026-11-17T15:10:00Z",
                "start_latlng": [50.1, 8.7],
                "distance": 15000,
                "elapsed_time": 3000,
            },
            self.profile,
        )

        weather = ride.weather_data
        self.assertEqual(weather["time"][0], "2026-11-17T16:00")
        self.assertIn(1.4, weather["precipitation"])


class RecomputeWindWithoutStreamTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(
            username="ohne-stream", password="pw"
        )
        profile = StravaProfile.objects.create(
            user=user,
            strava_athlete_id=778,
            access_token="t",
            refresh_token="r",
            expires_at=0,
            weather_consent_at=timezone.now(),
        )
        self.ride = Ride.objects.create(
            strava_id=9002,
            name="Ohne Stream",
            athlete=profile,
            start_latlng=Point(8.7, 50.1, srid=4326),
            start_date=datetime(2026, 11, 17, 16, 10, tzinfo=dt_timezone.utc),
            elapsed_time=3000,
            distance=15000,
        )

    @patch("app_dashboard.management.commands.recompute_wind.WeatherService")
    def test_backfill_covers_rides_without_gps_stream(self, mock_service):
        # Vorher filterte der Backfill auf vorhandene Streams; Rolle, manuelle Fahrten
        # und Importe mit fehlgeschlagenem Stream-Abruf behielten das verschobene Wetter.
        mock_service.get_historical_weather.return_value = _local_hourly(
            "2026-11-17", {17: 1.4}
        )
        mock_service.get_ride_rain.return_value = None

        call_command("recompute_wind", stdout=StringIO())

        self.ride.refresh_from_db()
        self.assertIn(1.4, self.ride.weather_data["precipitation"])
