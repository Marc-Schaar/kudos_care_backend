"""
Tests fuer die Gegenwind-Berechnung (app_dashboard/api/wind.py).

Kern der Suite ist `OutAndBackTests`: eine Hin-und-Rueck-Route bei konstantem Wind.
Genau dieser Fall war vorher kaputt — die alte Berechnung nahm die Luftlinie vom
ersten zum letzten GPS-Punkt, bei einer Rundfahrt also faktisch einen Zufallskurs,
und die Karte faerbte nach Segment-Index statt nach tatsaechlicher Richtung.
"""

from datetime import datetime, timezone

from django.test import SimpleTestCase

from app_dashboard.api.wind import (
    WIND_SOURCE_COARSE,
    WIND_SOURCE_STREAM,
    average_headwind,
    build_coarse_wind_segment,
    build_wind_segments,
    calculate_headwind,
    haversine_km,
    hourly_from_weather_data,
    hourly_headwind,
    segments_to_geojson,
)

START = datetime(2024, 6, 1, 10, 0, tzinfo=timezone.utc)


def _hourly(direction=0.0, speed=20.0, hours=4):
    """Konstanter Wind ueber `hours` Stunden ab 10:00 UTC."""
    return {
        "time": [f"2024-06-01T{10 + h:02d}:00" for h in range(hours)],
        "wind_direction_10m": [direction] * hours,
        "wind_speed_10m": [speed] * hours,
        "precipitation": [0.0] * hours,
        "temperature_2m": [18.0] * hours,
    }


def _north_leg(count, start_lat=50.0, lon=7.0, step=0.002):
    """`count` Punkte streng nach Norden."""
    return [[start_lat + i * step, lon] for i in range(count)]


class HaversineTests(SimpleTestCase):
    def test_one_degree_latitude_is_about_111_km(self):
        self.assertAlmostEqual(haversine_km(50.0, 7.0, 51.0, 7.0), 111.19, places=1)

    def test_identical_points_have_zero_distance(self):
        self.assertEqual(haversine_km(50.0, 7.0, 50.0, 7.0), 0.0)


class OutAndBackTests(SimpleTestCase):
    """
    Route: 20 Punkte nach Norden, dann dieselbe Strecke zurueck nach Sueden.
    Wind kommt konstant aus Norden (0 Grad) mit 20 km/h.
    Erwartung: Hinweg Gegenwind (+20), Rueckweg Rueckenwind (-20), Mittel ~0.
    """

    def setUp(self):
        out = _north_leg(20)
        back = list(reversed(out))[1:]
        self.latlngs = out + back
        self.times = list(range(0, len(self.latlngs) * 60, 60))
        self.segments = build_wind_segments(
            self.latlngs,
            self.times,
            START,
            _hourly(direction=0.0, speed=20.0),
            target_segments=10,
        )

    def test_produces_segments(self):
        self.assertEqual(len(self.segments), 10)

    def test_outbound_is_headwind_and_return_is_tailwind(self):
        half = len(self.segments) // 2
        outbound = self.segments[: half - 1]
        inbound = self.segments[half + 1 :]

        for segment in outbound:
            self.assertAlmostEqual(segment["headwind"], 20.0, delta=0.5)
        for segment in inbound:
            self.assertAlmostEqual(segment["headwind"], -20.0, delta=0.5)

    def test_average_cancels_out_over_the_full_loop(self):
        self.assertAlmostEqual(average_headwind(self.segments), 0.0, delta=1.0)

    def test_bearings_reflect_the_actual_direction_travelled(self):
        half = len(self.segments) // 2
        self.assertAlmostEqual(self.segments[0]["bearing"], 0.0, delta=1.0)
        self.assertAlmostEqual(self.segments[-1]["bearing"], 180.0, delta=1.0)

    def test_segments_are_contiguous(self):
        """Ende eines Abschnitts == Anfang des naechsten, sonst hat die Linie Luecken."""
        for previous, following in zip(self.segments, self.segments[1:]):
            self.assertEqual(previous["coords"][-1], following["coords"][0])


class StraightLineTests(SimpleTestCase):
    def test_full_headwind_when_wind_comes_from_ahead(self):
        latlngs = _north_leg(30)
        times = list(range(0, 30 * 60, 60))
        segments = build_wind_segments(
            latlngs, times, START, _hourly(direction=0.0, speed=15.0), target_segments=5
        )
        for segment in segments:
            self.assertAlmostEqual(segment["headwind"], 15.0, delta=0.5)
        self.assertAlmostEqual(average_headwind(segments), 15.0, delta=0.5)

    def test_crosswind_yields_near_zero_headwind(self):
        latlngs = _north_leg(30)
        times = list(range(0, 30 * 60, 60))
        segments = build_wind_segments(
            latlngs,
            times,
            START,
            _hourly(direction=90.0, speed=25.0),
            target_segments=5,
        )
        for segment in segments:
            self.assertAlmostEqual(segment["headwind"], 0.0, delta=0.5)


class DegenerateInputTests(SimpleTestCase):
    def setUp(self):
        self.latlngs = _north_leg(10)
        self.times = list(range(0, 600, 60))

    def test_missing_stream_returns_empty(self):
        self.assertEqual(build_wind_segments([], [], START, _hourly()), [])

    def test_mismatched_lengths_return_empty(self):
        self.assertEqual(
            build_wind_segments(self.latlngs, self.times[:-1], START, _hourly()), []
        )

    def test_missing_hourly_returns_empty(self):
        self.assertEqual(build_wind_segments(self.latlngs, self.times, START, {}), [])

    def test_stationary_track_returns_empty(self):
        stationary = [[50.0, 7.0]] * 10
        self.assertEqual(
            build_wind_segments(stationary, self.times, START, _hourly()), []
        )

    def test_never_more_segments_than_point_gaps(self):
        segments = build_wind_segments(
            self.latlngs, self.times, START, _hourly(), target_segments=500
        )
        self.assertLessEqual(len(segments), len(self.latlngs) - 1)


class DirectionInterpolationTests(SimpleTestCase):
    """
    Windrichtung muss zirkulaer interpoliert werden: zwischen 350 und 10 Grad liegt 0,
    nicht 180. Linear interpoliert waere die Richtung in der Mitte exakt
    entgegengesetzt — und das ausgerechnet, wenn sich der Wind kaum dreht.
    """

    def test_wraps_around_north_instead_of_through_south(self):
        hourly = {
            "time": ["2024-06-01T10:00", "2024-06-01T11:00"],
            "wind_direction_10m": [350.0, 10.0],
            "wind_speed_10m": [20.0, 20.0],
            "precipitation": [0.0, 0.0],
            "temperature_2m": [18.0, 18.0],
        }
        latlngs = _north_leg(120)
        times = list(range(0, 120 * 60, 60))
        segments = build_wind_segments(
            latlngs, times, START, hourly, target_segments=20
        )

        directions = [s["wind_direction"] for s in segments]
        # Alle Werte muessen nahe Nord liegen (<=15 Grad Abweichung), keiner Richtung Sued.
        for direction in directions:
            offset = min(direction, 360 - direction)
            self.assertLessEqual(
                offset, 15.0, f"Richtung {direction} zeigt nach Sueden"
            )


class CoarseFallbackTests(SimpleTestCase):
    def test_builds_single_segment_from_track(self):
        track = [(7.0, 50.0), (7.0, 50.05)]
        segments = build_coarse_wind_segment(track, START, 3600, _hourly(0.0, 20.0))
        self.assertEqual(len(segments), 1)
        self.assertAlmostEqual(segments[0]["headwind"], 20.0, delta=0.5)

    def test_returns_empty_for_loop_track(self):
        """Start == Ziel: kein sinnvoller Kurs ableitbar, lieber gar keine Aussage."""
        track = [(7.0, 50.0), (7.0, 50.05), (7.0, 50.0)]
        self.assertEqual(build_coarse_wind_segment(track, START, 3600, _hourly()), [])

    def test_returns_empty_without_hourly(self):
        track = [(7.0, 50.0), (7.0, 50.05)]
        self.assertEqual(build_coarse_wind_segment(track, START, 3600, {}), [])


class HourlyHeadwindTests(SimpleTestCase):
    def test_uses_segment_bearings_so_chart_matches_map(self):
        latlngs = _north_leg(30)
        times = list(range(0, 30 * 60, 60))
        hourly = _hourly(direction=0.0, speed=20.0, hours=2)
        segments = build_wind_segments(latlngs, times, START, hourly, target_segments=5)

        values = hourly_headwind(segments, hourly, START)
        self.assertEqual(len(values), 2)
        for value in values:
            self.assertAlmostEqual(value, 20.0, delta=0.5)

    def test_returns_zeros_without_segments(self):
        hourly = _hourly(hours=3)
        self.assertEqual(hourly_headwind([], hourly, START), [0.0, 0.0, 0.0])


class AverageHeadwindTests(SimpleTestCase):
    def test_weights_by_distance_not_by_segment_count(self):
        segments = [
            {"headwind": 10.0, "distance_km": 9.0},
            {"headwind": 0.0, "distance_km": 1.0},
        ]
        self.assertEqual(average_headwind(segments), 9.0)

    def test_ignores_segments_without_headwind(self):
        segments = [
            {"headwind": None, "distance_km": 5.0},
            {"headwind": 4.0, "distance_km": 5.0},
        ]
        self.assertEqual(average_headwind(segments), 4.0)

    def test_returns_none_when_nothing_usable(self):
        self.assertIsNone(average_headwind([]))
        self.assertIsNone(average_headwind([{"headwind": None, "distance_km": 1.0}]))


class WeatherDataRoundTripTests(SimpleTestCase):
    def test_reconstructs_hourly_block(self):
        weather_data = _hourly(direction=180.0, speed=12.0, hours=2)
        weather_data["headwind"] = [1.0, 2.0]
        weather_data["avg_headwind"] = 1.5

        rebuilt = hourly_from_weather_data(weather_data)
        self.assertEqual(rebuilt["wind_direction_10m"], [180.0, 180.0])
        self.assertNotIn("headwind", rebuilt)

    def test_legacy_data_without_direction_yields_empty(self):
        """Altdaten vor dem Backfill: lieber grober Fallback als erfundene Richtung."""
        legacy = {
            "time": ["2024-06-01T10:00"],
            "wind_speed_10m": [10.0],
            "precipitation": [0.0],
            "temperature_2m": [18.0],
        }
        self.assertEqual(hourly_from_weather_data(legacy), {})


class GeoJsonTests(SimpleTestCase):
    def test_emits_one_feature_per_segment_with_wind_source(self):
        latlngs = _north_leg(20)
        times = list(range(0, 20 * 60, 60))
        segments = build_wind_segments(
            latlngs, times, START, _hourly(), target_segments=4
        )

        collection = segments_to_geojson(segments, WIND_SOURCE_STREAM)
        self.assertEqual(collection["type"], "FeatureCollection")
        self.assertEqual(collection["wind_source"], WIND_SOURCE_STREAM)
        self.assertEqual(len(collection["features"]), 4)

        properties = collection["features"][0]["properties"]
        for key in (
            "headwind",
            "wind_speed",
            "wind_direction",
            "bearing",
            "precipitation",
        ):
            self.assertIn(key, properties)

    def test_empty_segments_yield_empty_collection(self):
        collection = segments_to_geojson([], WIND_SOURCE_COARSE)
        self.assertEqual(collection["features"], [])


class HeadwindSignConventionTests(SimpleTestCase):
    """Vorzeichen-Konvention festnageln — die UI faerbt danach ein."""

    def test_wind_from_ahead_is_positive(self):
        self.assertEqual(calculate_headwind(0.0, 0.0, 20.0), 20.0)

    def test_wind_from_behind_is_negative(self):
        self.assertEqual(calculate_headwind(0.0, 180.0, 20.0), -20.0)

    def test_pure_crosswind_is_zero(self):
        self.assertAlmostEqual(calculate_headwind(0.0, 90.0, 20.0), 0.0, delta=0.01)
