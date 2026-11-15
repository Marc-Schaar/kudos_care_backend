"""
Die Prognose im Dashboard (`GET maintenance/bikes/<id>/next-ride/`): wann fährt man
voraussichtlich wieder, und was wird bis dahin fällig.
"""

import itertools
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from app_auth.models import StravaProfile
from app_dashboard.models import Ride

from .models import (
    Bike,
    BikeType,
    Component,
    ComponentCategory,
    ComponentSlot,
    ComponentTemplate,
    MaintenanceInterval,
)


class NextRideForecastTests(APITestCase):
    def setUp(self) -> None:
        self.user = get_user_model().objects.create_user(username="nr", password="pw")
        self.profile = StravaProfile.objects.create(
            user=self.user,
            strava_athlete_id=81001,
            access_token="t",
            refresh_token="r",
            expires_at=0,
        )
        self.bike = Bike.objects.create(
            athlete=self.profile,
            strava_bike_id="nr1",
            name="Gravel",
            bike_type=BikeType.GRAVEL,
        )
        self._ids = itertools.count(95000)
        self.client.force_login(self.user)
        session = self.client.session
        session["strava_athlete_id"] = self.profile.strava_athlete_id
        session.save()

    def _rides(self, count: int = 5, gap_days: int = 7, last_days_ago: int = 6) -> None:
        now = timezone.now()
        for i in range(count):
            Ride.objects.create(
                strava_id=next(self._ids),
                name="Fahrt",
                distance=20000,
                start_date=now - timedelta(days=last_days_ago + i * gap_days),
                athlete=self.profile,
                bike=self.bike,
            )

    def _part(self, warn_km: float, name: str = "Kette") -> Component:
        template = ComponentTemplate.objects.create(
            name=name, category=ComponentCategory.DRIVETRAIN, warn_km=warn_km
        )
        slot = ComponentSlot.objects.create(bike=self.bike, template=template)
        return Component.objects.create(
            slot=slot, brand="Test", installed_at=date.today() - timedelta(days=60)
        )

    def _get(self, bike: Bike | None = None):
        return self.client.get(
            reverse("bike-next-ride", kwargs={"pk": (bike or self.bike).pk})
        )

    def test_without_enough_rides_there_is_no_prediction(self) -> None:
        self._rides(count=2)
        res = self._get()
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.data["predicted_date"])
        self.assertEqual(res.data["ride_count"], 2)
        self.assertEqual(res.data["min_rides"], 4)

    def test_predicts_date_distance_and_days_until(self) -> None:
        self._rides()  # letzte Fahrt vor 6 Tagen, alle 7 Tage -> morgen
        res = self._get()
        self.assertEqual(res.data["predicted_date"], date.today() + timedelta(days=1))
        self.assertEqual(res.data["days_until"], 1)
        self.assertEqual(res.data["predicted_km"], 20.0)
        self.assertFalse(res.data["is_overdue"])
        self.assertEqual(res.data["rides_considered"], 5)

    def test_part_crossing_its_limit_with_the_next_ride_is_listed(self) -> None:
        self._rides()  # 100 km bisher, nächste Fahrt ~20 km
        chain = self._part(warn_km=120)  # heute 0,83 -> warn, danach 1,0 -> critical
        self._part(warn_km=5000, name="Kurbel")  # bleibt weit weg
        res = self._get()
        self.assertEqual(
            res.data["items"],
            [
                {
                    "kind": "part",
                    "id": chain.pk,
                    "status_today": "warn",
                    "status_then": "critical",
                }
            ],
        )

    def test_interval_is_projected_with_the_next_ride_distance(self) -> None:
        self._rides()
        interval = MaintenanceInterval.objects.create(
            bike=self.bike, label="Kette ölen", interval_km=200
        )  # heute 100/200 = ok, danach 120/200 = ok
        oil = MaintenanceInterval.objects.create(
            bike=self.bike, label="Dichtmilch", interval_km=115
        )  # heute 100/115 = warn, danach 120/115 = critical
        res = self._get()
        ids = {item["id"] for item in res.data["items"] if item["kind"] == "interval"}
        self.assertNotIn(interval.pk, ids)
        self.assertIn(oil.pk, ids)

    def test_overdue_rhythm_is_flagged_instead_of_pretending_today(self) -> None:
        self._rides(last_days_ago=40)
        res = self._get()
        self.assertEqual(res.data["predicted_date"], date.today())
        self.assertTrue(res.data["is_overdue"])

    def test_foreign_bike_is_404(self) -> None:
        other_user = get_user_model().objects.create_user(username="o", password="pw")
        other = StravaProfile.objects.create(
            user=other_user,
            strava_athlete_id=81002,
            access_token="t",
            refresh_token="r",
            expires_at=0,
        )
        bike = Bike.objects.create(
            athlete=other, strava_bike_id="o1", name="Fremd", bike_type=BikeType.ROAD
        )
        self.assertEqual(self._get(bike).status_code, 404)

    def test_requires_login(self) -> None:
        self.client.logout()
        self.assertEqual(self._get().status_code, 403)
