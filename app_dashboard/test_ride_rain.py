"""Regen während der Fahrt: 15-Minuten-Werte, Stunden-Fallback und ihre Abnehmer."""

from datetime import datetime
from datetime import timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from django.test import SimpleTestCase

from app_dashboard.api.ride_rain import (
    MAX_ROUTE_LOCATIONS,
    SOURCE_ROUTE,
    SOURCE_START_POINT,
    apply_ride_rain,
    build_ride_rain,
    merge_route_minutely,
    ride_precipitation,
    route_rain_locations,
)
from app_dashboard.api.services import WeatherService
from app_maintenance.api.services import WeatherWearCalculator

START = datetime(2026, 11, 18, 16, 10, tzinfo=dt_timezone.utc)


def _quarters(values: dict[str, float | None]) -> dict:
    return {"time": list(values), "precipitation": list(values.values())}


class BuildRideRainTests(SimpleTestCase):
    def test_value_counts_for_the_quarter_before_its_timestamp(self):
        # Fahrt 16:10–16:30. Der Wert von 16:15 gilt für 16:00–16:15, also 5 Minuten.
        rain = build_ride_rain(
            START,
            1200,
            _quarters(
                {
                    "2026-11-18T16:00": 9.0,
                    "2026-11-18T16:15": 0.3,
                    "2026-11-18T16:30": 0.0,
                    "2026-11-18T16:45": 9.0,
                }
            ),
        )
        self.assertEqual(rain["time"], ["2026-11-18T16:15", "2026-11-18T16:30"])
        self.assertEqual(rain["precipitation"], [1.2, 0.0])
        self.assertAlmostEqual(rain["mm_h"], 0.3, places=3)

    def test_missing_value_is_unknown_not_dry(self):
        minutely = _quarters({"2026-11-18T16:15": 0.1, "2026-11-18T16:30": None})
        self.assertIsNone(build_ride_rain(START, 1200, minutely))

    def test_ride_beyond_the_data_is_not_guessed(self):
        minutely = _quarters({"2026-11-18T16:15": 0.1})
        self.assertIsNone(build_ride_rain(START, 1200, minutely))


class RidePrecipitationTests(SimpleTestCase):
    HOURLY = {
        "time": ["2026-11-18T05:00", "2026-11-18T06:00", "2026-11-18T07:00"],
        "precipitation": [0.0, 0.7, 0.1],
    }
    EARLY = datetime(2026, 11, 18, 5, 10, 10, tzinfo=dt_timezone.utc)

    def test_hours_are_weighted_by_the_time_ridden(self):
        # Regressionstest: Der einfache Mittelwert dieser Fahrt wäre 0,27 mm/h.
        rain = ride_precipitation(self.HOURLY, self.EARLY, 6258)
        self.assertAlmostEqual(rain, 0.387, places=3)

    def test_quarter_hours_win_over_hours(self):
        data = {**self.HOURLY, "ride_rain": {"mm_h": 1.5}}
        self.assertEqual(ride_precipitation(data, self.EARLY, 6258), 1.5)

    def test_without_ride_times_it_stays_the_mean(self):
        self.assertAlmostEqual(ride_precipitation(self.HOURLY), 0.8 / 3)

    def test_no_data_stays_unknown(self):
        self.assertIsNone(ride_precipitation({}, self.EARLY, 6258))

    def test_wear_uses_the_rain_of_the_ride(self):
        coeff = SimpleNamespace(
            rain_sensitivity=1.0,
            heat_sensitivity=0.0,
            cold_sensitivity=0.0,
            wind_sensitivity=0.0,
        )
        weather = {
            **self.HOURLY,
            "temperature_2m": [9.0, 9.0, 9.0],
            "wind_speed_10m": [10.0, 10.0, 10.0],
        }
        multiplier = WeatherWearCalculator.ride_multiplier(
            weather, coeff, self.EARLY, 6258
        )
        self.assertAlmostEqual(multiplier, 1 + 0.387 / 2.5, places=3)


class ApplyRideRainTests(SimpleTestCase):
    def test_segments_take_the_quarter_they_were_ridden_in(self):
        segments = [
            {"t_start": 0, "t_end": 300, "precipitation": 0.0},
            {"t_start": 300, "t_end": 1200, "precipitation": 0.0},
            {"t_start": 3600, "t_end": 3900, "precipitation": 0.4},
        ]
        rain = {
            "time": ["2026-11-18T16:15", "2026-11-18T16:30"],
            "precipitation": [2.0, 0.0],
        }
        apply_ride_rain(segments, START, rain)
        self.assertEqual([s["precipitation"] for s in segments], [2.0, 0.0, 0.4])


def _stream(seconds: int = 1200) -> dict:
    """Fährt nach Norden, 0,001 Grad je Minute."""
    times = list(range(0, seconds + 1, 60))
    return {
        "latlng": {"data": [[50.0 + t / 60_000, 8.7] for t in times]},
        "time": {"data": times},
    }


class RouteRainLocationsTests(SimpleTestCase):
    def test_each_quarter_gets_the_rounded_position_of_that_moment(self):
        stream = {
            "latlng": {"data": [[50.123456, 8.654321], [50.987654, 8.123456]]},
            "time": {"data": [0, 600]},
        }
        locations = route_rain_locations(START, 1200, stream)
        self.assertEqual(
            locations,
            [
                ("2026-11-18T16:15", 50.99, 8.12),
                ("2026-11-18T16:30", 50.99, 8.12),
            ],
        )

    def test_long_rides_share_places(self):
        locations = route_rain_locations(START, 6 * 3600, _stream(6 * 3600))
        self.assertEqual(len(locations), 25)
        self.assertLessEqual(len({loc[1:] for loc in locations}), MAX_ROUTE_LOCATIONS)

    def test_without_gps_there_is_nothing_to_send(self):
        self.assertEqual(route_rain_locations(START, 1200, None), [])
        self.assertEqual(route_rain_locations(START, 1200, {}), [])

    def test_merge_takes_the_value_of_the_place_ridden_through(self):
        locations = [("16:15", 50.0, 8.0), ("16:30", 51.0, 8.0)]
        merged = merge_route_minutely(
            locations,
            {
                (50.0, 8.0): {"time": ["16:15", "16:30"], "precipitation": [0.1, 9.0]},
                (51.0, 8.0): {"time": ["16:15", "16:30"], "precipitation": [9.0, 0.2]},
            },
        )
        self.assertEqual(merged["precipitation"], [0.1, 0.2])


class RouteConsentTests(SimpleTestCase):
    MINUTELY = {
        "time": ["2026-11-18T16:15", "2026-11-18T16:30"],
        "precipitation": [0.1, 0.1],
    }

    def _ride(self, route_consent: bool) -> SimpleNamespace:
        athlete = SimpleNamespace(
            weather_consent_at=START,
            route_weather_consent_at=START if route_consent else None,
        )
        return SimpleNamespace(
            pk=1, start_date=START, elapsed_time=1200, athlete=athlete
        )

    @patch("app_dashboard.api.services.requests.get")
    def test_without_route_consent_only_the_start_point_leaves(self, mock_get):
        mock_get.return_value = Mock(json=lambda: {"minutely_15": self.MINUTELY})

        rain = WeatherService.get_ride_rain(self._ride(False), 50.1, 8.7, _stream())

        mock_get.assert_called_once()
        self.assertEqual(mock_get.call_args.kwargs["params"]["latitude"], "50.1")
        self.assertEqual(rain["source"], SOURCE_START_POINT)

    @patch("app_dashboard.api.services.requests.get")
    def test_with_route_consent_rounded_route_points_are_used(self, mock_get):
        mock_get.return_value = Mock(
            json=lambda: [
                {"minutely_15": self.MINUTELY},
                {"minutely_15": self.MINUTELY},
            ]
        )

        rain = WeatherService.get_ride_rain(self._ride(True), 50.1, 8.7, _stream())

        self.assertEqual(mock_get.call_args.kwargs["params"]["latitude"], "50.0,50.01")
        self.assertEqual(rain["source"], SOURCE_ROUTE)
        self.assertEqual(set(rain), {"source", "mm_h", "time", "precipitation"})

    @patch("app_dashboard.api.services.requests.get")
    def test_route_failure_falls_back_to_the_start_point(self, mock_get):
        mock_get.side_effect = [
            requests.exceptions.Timeout(),
            Mock(json=lambda: {"minutely_15": self.MINUTELY}),
        ]

        rain = WeatherService.get_ride_rain(self._ride(True), 50.1, 8.7, _stream())

        self.assertEqual(rain["source"], SOURCE_START_POINT)


class GetRideRainTests(SimpleTestCase):
    RIDE = SimpleNamespace(pk=1, start_date=START, elapsed_time=1200)

    @patch("app_dashboard.api.services.requests.get")
    def test_asks_for_the_start_point_only(self, mock_get):
        mock_get.return_value = Mock(
            json=lambda: {
                "minutely_15": _quarters(
                    {"2026-11-18T16:15": 0.1, "2026-11-18T16:30": 0.1}
                )
            }
        )

        rain = WeatherService.get_ride_rain(self.RIDE, 50.1, 8.7)

        params = mock_get.call_args.kwargs["params"]
        self.assertEqual((params["latitude"], params["longitude"]), ("50.1", "8.7"))
        self.assertEqual(params["minutely_15"], "precipitation")
        self.assertEqual(params["timezone"], "auto")
        self.assertAlmostEqual(rain["mm_h"], 0.4)

    @patch("app_dashboard.api.services.requests.get")
    def test_failure_falls_back_without_logging_coordinates(self, mock_get):
        mock_get.side_effect = requests.exceptions.ConnectionError(
            "latitude=50.1&longitude=8.7"
        )

        with self.assertLogs("app_dashboard.api.services", "WARNING") as logs:
            self.assertIsNone(WeatherService.get_ride_rain(self.RIDE, 50.1, 8.7))

        self.assertNotIn("50.1", "".join(logs.output))
