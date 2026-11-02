from datetime import date, timedelta

from rest_framework import status

from app_maintenance.models import (
    BikeType,
    MaintenanceInterval,
    MaintenanceLog,
)
from app_maintenance.testing import MaintenanceTestCase


class MaintenanceIntervalStatusTests(MaintenanceTestCase):
    bike_type = BikeType.GRAVEL

    def _interval(self, **kwargs):
        defaults = dict(
            bike=self.bike,
            kind="chain_lube",
            label="Kettenöl auffrischen",
            interval_km=300,
            interval_days=None,
            last_done_at=self.days_ago(10),
        )
        defaults.update(kwargs)
        return MaintenanceInterval.objects.create(**defaults)

    def test_status_ok_below_threshold(self):
        iv = self._interval()
        self.ride(100, days_ago=2)  # 100 / 300 = 0.33
        self.assertEqual(iv.status(), "ok")

    def test_status_warn_near_threshold(self):
        iv = self._interval()
        self.ride(250, days_ago=2)  # 250 / 300 = 0.83
        self.assertEqual(iv.status(), "warn")

    def test_status_critical_over_threshold(self):
        iv = self._interval()
        self.ride(400, days_ago=2)  # 400 / 300 = 1.33
        self.assertEqual(iv.status(), "critical")

    def test_rides_before_the_last_done_date_do_not_count(self):
        iv = self._interval()
        self.ride(400, days_ago=20)
        self.assertEqual(iv.km_since_done(), 0.0)
        self.assertEqual(iv.status(), "ok")

    def test_status_days_axis(self):
        iv = self._interval(
            interval_km=None,
            interval_days=100,
            last_done_at=date.today() - timedelta(days=120),
        )
        self.assertEqual(iv.status(), "critical")

    def test_status_as_of_projects_days_axis(self):
        iv = self._interval(
            interval_km=None,
            interval_days=100,
            last_done_at=date.today() - timedelta(days=50),
        )
        self.assertEqual(iv.status(), "ok")
        future = date.today() + timedelta(days=60)  # 110 / 100
        self.assertEqual(iv.status(as_of=future), "critical")

    def test_status_unknown_without_thresholds(self):
        iv = self._interval(interval_km=None, interval_days=None)
        self.assertEqual(iv.status(), "unknown")


class MaintenanceIntervalLogViewTests(MaintenanceTestCase):
    bike_type = BikeType.GRAVEL

    def setUp(self):
        super().setUp()
        self.ride(500, days_ago=5)
        self.interval = MaintenanceInterval.objects.create(
            bike=self.bike,
            kind="sealant",
            label="Dichtmilch",
            interval_days=120,
            last_done_at=date.today() - timedelta(days=200),
        )

    def test_log_resets_baseline_and_appends_log(self):
        self.assertEqual(self.interval.status(), "critical")

        res = self.client.post(
            f"/api/maintenance/intervals/{self.interval.id}/log/", {}, format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["status"], "ok")
        self.assertEqual(res.data["km_since"], 0.0)

        self.interval.refresh_from_db()
        self.assertEqual(self.interval.last_done_at, self.today())
        self.assertEqual(
            MaintenanceLog.objects.filter(interval=self.interval).count(), 1
        )

    def test_log_accepts_explicit_values(self):
        res = self.client.post(
            f"/api/maintenance/intervals/{self.interval.id}/log/",
            {"done_at": str(self.days_ago(3)), "note": "vor Ort"},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        log = MaintenanceLog.objects.get(interval=self.interval)
        self.assertEqual(log.done_at, self.days_ago(3))
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
        self.login(self.make_profile())

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
        self.assertEqual(res.data["km_since"], 0.0)

        res = self.client.delete(f"/api/maintenance/intervals/{iv_id}/")
        self.assertEqual(res.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(MaintenanceInterval.objects.filter(id=iv_id).exists())
