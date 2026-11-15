"""
Tests für die Verteilung des Fahrt-Aufschlags auf die Streckenabschnitte
(`app_maintenance/api/services.py::attach_segment_wear`).

Kernaussage: die Abschnitte verteilen den Aufschlag nur, sie rechnen ihn nicht neu. Die
Summe ist immer der `extra_km` der Kategorie aus `ride_wear_breakdown()`, sonst nennte die
Fahrt-Seite am Ende des Verlaufs eine andere Zahl als in der Aufschlüsselung darunter.
"""

from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from app_auth.models import StravaProfile
from app_dashboard.models import Ride, RideStream
from app_maintenance.api.services import attach_segment_wear
from app_maintenance.models import (
    Bike,
    BikeType,
    Component,
    ComponentCategory,
    ComponentSlot,
    ComponentTemplate,
    WeatherSensitivityCoefficient,
)


def _segment(km, precipitation=0.0, temperature=15.0, wind_speed=10.0):
    return {
        "distance_km": km,
        "precipitation": precipitation,
        "temperature": temperature,
        "wind_speed": wind_speed,
    }


def _breakdown(extra_km):
    return {
        "categories": [
            {"category": ComponentCategory.BRAKES, "extra_km": extra_km},
        ]
    }


class AttachSegmentWearTests(TestCase):
    def setUp(self):
        WeatherSensitivityCoefficient.objects.update_or_create(
            category=ComponentCategory.BRAKES,
            defaults={
                "rain_sensitivity": 0.9,
                "heat_sensitivity": 0.0,
                "cold_sensitivity": 0.0,
                "wind_sensitivity": 0.0,
            },
        )

    def _profile(self, extra_km, segments):
        return attach_segment_wear(_breakdown(extra_km), segments)["categories"][0][
            "segment_extra_km"
        ]

    def test_sum_is_the_category_surcharge(self):
        profile = self._profile(
            7.3, [_segment(5, 0.0), _segment(5, 3.0), _segment(5, 1.0)]
        )
        self.assertEqual(len(profile), 3)
        self.assertAlmostEqual(sum(profile), 7.3, places=3)

    def test_surcharge_lands_where_it_rained(self):
        profile = self._profile(4.0, [_segment(10, 0.0), _segment(10, 4.0)])
        self.assertEqual(profile[0], 0.0)
        self.assertAlmostEqual(profile[1], 4.0, places=3)

    def test_heavier_rain_carries_more_of_the_surcharge(self):
        profile = self._profile(6.0, [_segment(10, 1.0), _segment(10, 2.0)])
        self.assertGreater(profile[1], profile[0])

    def test_without_segment_weather_it_spreads_by_length(self):
        """Altfahrt ohne Abschnittswetter: der Aufschlag verschwindet nicht, er verteilt sich."""
        blank = {"precipitation": None, "temperature": None, "wind_speed": None}
        profile = self._profile(
            3.0, [{"distance_km": 10, **blank}, {"distance_km": 20, **blank}]
        )
        self.assertAlmostEqual(profile[0], 1.0, places=3)
        self.assertAlmostEqual(profile[1], 2.0, places=3)

    def test_dry_ride_gets_zeros(self):
        self.assertEqual(self._profile(0.0, [_segment(5), _segment(5)]), [0.0, 0.0])

    def test_no_segments_gives_an_empty_list(self):
        self.assertEqual(self._profile(2.0, []), [])


class WearImpactSegmentProfileViewTests(APITestCase):
    """Der Endpoint liefert die Verteilung im selben Index wie `wind_segments`."""

    def setUp(self):
        user = get_user_model().objects.create_user(username="segment-wear")
        self.profile = StravaProfile.objects.create(
            user=user,
            strava_athlete_id=445566,
            access_token="t",
            refresh_token="r",
            expires_at=0,
        )
        bike = Bike.objects.create(
            athlete=self.profile,
            strava_bike_id="segment-bike",
            name="Segment Bike",
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
            name="Bremsbelag vorne", category=ComponentCategory.BRAKES, warn_km=1000
        )
        slot = ComponentSlot.objects.create(bike=bike, template=template)
        start = (timezone.now() - timedelta(days=5)).replace(
            minute=0, second=0, microsecond=0
        )
        Component.objects.create(
            slot=slot, installed_at=(start - timedelta(days=30)).date()
        )

        hours = [
            (start + timedelta(hours=h)).astimezone(dt_timezone.utc) for h in range(3)
        ]
        self.ride = Ride.objects.create(
            strava_id=9001,
            name="Schauer am Ende",
            distance=20_000,
            start_date=start,
            elapsed_time=7200,
            athlete=self.profile,
            bike=bike,
            weather_data={
                "time": [datetime.strftime(h, "%Y-%m-%dT%H:%M") for h in hours],
                "wind_direction_10m": [180.0, 180.0, 180.0],
                "wind_speed_10m": [10.0, 10.0, 10.0],
                "temperature_2m": [14.0, 14.0, 14.0],
                # Trocken los, Regen erst in der zweiten Stunde.
                "precipitation": [0.0, 0.0, 5.0],
            },
        )
        # Zwei Stunden geradeaus nach Norden, ~20 km.
        RideStream.objects.create(
            ride=self.ride,
            latlngs=[[50.0 + i * 0.0018, 8.0] for i in range(101)],
            time_series=[i * 72 for i in range(101)],
        )

        self.client.force_login(user)
        session = self.client.session
        session["strava_athlete_id"] = self.profile.strava_athlete_id
        session.save()

    def test_profile_matches_wind_segments_and_sums_to_the_surcharge(self):
        impact = self.client.get(f"/api/activities/{self.ride.id}/wear-impact/")
        detail = self.client.get(f"/api/activities/{self.ride.id}/")
        self.assertEqual(impact.status_code, status.HTTP_200_OK)

        brakes = impact.data["breakdown"]["categories"][0]
        profile = brakes["segment_extra_km"]
        features = detail.data["wind_segments"]["features"]

        self.assertEqual(len(profile), len(features))
        self.assertGreater(len(profile), 1)
        self.assertAlmostEqual(sum(profile), brakes["extra_km"], places=2)
        # Der Schauer kam am Ende: die erste Hälfte trägt nichts, die zweite alles.
        half = len(profile) // 2
        self.assertEqual(sum(profile[: half // 2]), 0.0)
        self.assertGreater(sum(profile[half:]), sum(profile[:half]))
