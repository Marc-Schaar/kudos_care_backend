from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from app_auth.models import StravaProfile
from app_dashboard.models import Ride
from app_maintenance.models import (
    Bike,
    BikeType,
    MaintenanceInterval,
    MaintenanceLog,
)


def _make_profile(user, strava_athlete_id=33221):
    return StravaProfile.objects.create(
        user=user,
        strava_athlete_id=strava_athlete_id,
        access_token="token",
        refresh_token="refresh",
        expires_at=0,
    )


class MaintenanceIntervalStatusTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="iv", password="pw")
        self.profile = _make_profile(self.user)
        self.client.force_login(self.user)
        session = self.client.session
        session["strava_athlete_id"] = self.profile.strava_athlete_id
        session.save()

        self.bike = Bike.objects.create(
            athlete=self.profile,
            strava_bike_id="iv1",
            name="Rad",
            bike_type=BikeType.GRAVEL,
        )
        Ride.objects.create(
            strava_id=7001,
            name="Fahrt",
            distance=1_000_000,  # 1000 km
            start_date=timezone.now() - timedelta(days=5),
            athlete=self.profile,
            bike=self.bike,
        )

    def _interval(self, **kwargs):
        defaults = dict(
            bike=self.bike,
            kind="chain_lube",
            label="Kettenöl auffrischen",
            interval_km=300,
            interval_days=None,
            last_done_at=date.today(),
            last_done_distance_km=900,
        )
        defaults.update(kwargs)
        return MaintenanceInterval.objects.create(**defaults)

    def test_status_ok_below_threshold(self):
        iv = self._interval(last_done_distance_km=900)  # 100 / 300 = 0.33
        self.assertEqual(iv.status(self.bike.total_distance_km), "ok")

    def test_status_warn_near_threshold(self):
        iv = self._interval(last_done_distance_km=750)  # 250 / 300 = 0.83
        self.assertEqual(iv.status(self.bike.total_distance_km), "warn")

    def test_status_critical_over_threshold(self):
        iv = self._interval(last_done_distance_km=600)  # 400 / 300 = 1.33
        self.assertEqual(iv.status(self.bike.total_distance_km), "critical")

    def test_status_days_axis(self):
        iv = self._interval(
            interval_km=None,
            interval_days=100,
            last_done_at=date.today() - timedelta(days=120),
        )
        self.assertEqual(iv.status(self.bike.total_distance_km), "critical")

    def test_status_as_of_projects_days_axis(self):
        iv = self._interval(
            interval_km=None,
            interval_days=100,
            last_done_at=date.today() - timedelta(days=50),
        )
        self.assertEqual(iv.status(self.bike.total_distance_km), "ok")
        future = date.today() + timedelta(days=60)  # 110 / 100
        self.assertEqual(
            iv.status(self.bike.total_distance_km, as_of=future), "critical"
        )

    def test_status_unknown_without_thresholds(self):
        iv = self._interval(interval_km=None, interval_days=None)
        self.assertEqual(iv.status(self.bike.total_distance_km), "unknown")


class MaintenanceIntervalLogViewTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="ivlog", password="pw"
        )
        self.profile = _make_profile(self.user, strava_athlete_id=33222)
        self.client.force_login(self.user)
        session = self.client.session
        session["strava_athlete_id"] = self.profile.strava_athlete_id
        session.save()

        self.bike = Bike.objects.create(
            athlete=self.profile,
            strava_bike_id="ivl1",
            name="Rad",
            bike_type=BikeType.GRAVEL,
        )
        Ride.objects.create(
            strava_id=7101,
            name="Fahrt",
            distance=500_000,  # 500 km
            start_date=timezone.now() - timedelta(days=5),
            athlete=self.profile,
            bike=self.bike,
        )
        self.interval = MaintenanceInterval.objects.create(
            bike=self.bike,
            kind="sealant",
            label="Dichtmilch",
            interval_days=120,
            last_done_at=date.today() - timedelta(days=200),
            last_done_distance_km=100,
        )

    def test_log_resets_baseline_and_appends_log(self):
        self.assertEqual(self.interval.status(self.bike.total_distance_km), "critical")

        res = self.client.post(
            f"/api/maintenance/intervals/{self.interval.id}/log/", {}, format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["status"], "ok")

        self.interval.refresh_from_db()
        self.assertEqual(self.interval.last_done_at, date.today())
        self.assertEqual(self.interval.last_done_distance_km, 500.0)
        self.assertEqual(
            MaintenanceLog.objects.filter(interval=self.interval).count(), 1
        )

    def test_log_accepts_explicit_values(self):
        res = self.client.post(
            f"/api/maintenance/intervals/{self.interval.id}/log/",
            {
                "done_at": str(date.today() - timedelta(days=3)),
                "done_distance_km": 480,
                "note": "vor Ort",
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        log = MaintenanceLog.objects.get(interval=self.interval)
        self.assertEqual(log.done_distance_km, 480)
        self.assertEqual(log.note, "vor Ort")

    def test_auth_required(self):
        self.client.logout()
        res = self.client.post(
            f"/api/maintenance/intervals/{self.interval.id}/log/", {}, format="json"
        )
        self.assertIn(
            res.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN)
        )

    def test_other_athlete_cannot_log(self):
        other = get_user_model().objects.create_user(username="other", password="pw")
        _make_profile(other, strava_athlete_id=99999)
        self.client.force_login(other)
        session = self.client.session
        session["strava_athlete_id"] = 99999
        session.save()

        res = self.client.post(
            f"/api/maintenance/intervals/{self.interval.id}/log/", {}, format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

    def test_add_and_delete_adhoc_interval(self):
        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/intervals/",
            {"label": "Federgabel Luftdruck", "interval_days": 30},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        iv_id = res.data["id"]
        self.assertEqual(res.data["last_done_distance_km"], 500.0)

        res = self.client.delete(f"/api/maintenance/intervals/{iv_id}/")
        self.assertEqual(res.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(MaintenanceInterval.objects.filter(id=iv_id).exists())
