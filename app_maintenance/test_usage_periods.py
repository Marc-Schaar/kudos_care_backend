"""
Reine Zeitfenster-Logik aus `api/usage.py` — ohne DB, analog zu `app_dashboard/test_wind.py`.
Dazu die Umrechnung des Bestands aus Migration `0025`, an der hängt, dass sich beim Deploy
keine angezeigten km verschieben.
"""

from datetime import date, datetime, timedelta, timezone
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from app_maintenance.api.usage import (
    RideLedger,
    clip_windows,
    date_in_windows,
    intersect_windows,
    moment_for,
    ride_clock_now,
    start_of_day,
)

UTC = timezone.utc


def at(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 6, day, hour, tzinfo=UTC)


LEDGER = RideLedger([(at(1), 10.0), (at(2), 20.0), (at(3), 30.0)])


class RideLedgerTests(SimpleTestCase):
    def test_counts_rides_inside_the_window(self):
        self.assertEqual(LEDGER.km_between(at(1), at(3)), 30.0)

    def test_window_is_half_open(self):
        """Beim Tausch endet A genau dort, wo B beginnt: die Fahrt gehört nur B."""
        swap = at(2)
        self.assertEqual(LEDGER.km_between(at(1), swap), 10.0)
        self.assertEqual(LEDGER.km_between(swap, None), 50.0)

    def test_open_ends_reach_everything(self):
        self.assertEqual(LEDGER.km_between(None, None), 60.0)
        self.assertEqual(LEDGER.km_between(None, at(2)), 10.0)
        self.assertEqual(LEDGER.km_between(at(3), None), 30.0)

    def test_empty_and_inverted_windows_are_zero(self):
        self.assertEqual(LEDGER.km_between(at(3), at(1)), 0.0)
        self.assertEqual(RideLedger().km_between(None, None), 0.0)

    def test_rides_need_not_arrive_in_order(self):
        ledger = RideLedger([(at(3), 30.0), (at(1), 10.0), (at(2), 20.0)])
        self.assertEqual(ledger.km_between(at(1), at(3)), 30.0)

    def test_gap_between_windows_does_not_count(self):
        """Der Kernfall des Laufradwechsels: die Zeit im Keller fällt heraus."""
        self.assertEqual(LEDGER.km_in([(at(1), at(2)), (at(3), None)]), 40.0)


class ClipWindowsTests(SimpleTestCase):
    def test_no_baseline_changes_nothing(self):
        windows = [(at(1), at(2))]
        self.assertEqual(clip_windows(windows, None), windows)

    def test_cuts_a_window_at_the_baseline(self):
        self.assertEqual(clip_windows([(at(1), at(3))], at(2)), [(at(2), at(3))])

    def test_drops_windows_that_end_before_the_baseline(self):
        self.assertEqual(
            clip_windows([(at(1), at(2)), (at(3), None)], at(2)), [(at(3), None)]
        )

    def test_open_start_gets_the_baseline(self):
        self.assertEqual(clip_windows([(None, None)], at(2)), [(at(2), None)])


class IntersectWindowsTests(SimpleTestCase):
    def test_backdating_skips_the_time_in_the_cellar(self):
        """Ein Reifen ab Tag 2, das Laufrad war Tag 1–3 und ab Tag 5 am Rad."""
        wheel = [(at(1), at(3)), (at(5), None)]
        self.assertEqual(
            intersect_windows((at(2), None), wheel),
            [(at(2), at(3)), (at(5), None)],
        )

    def test_part_ends_while_the_wheel_is_still_on(self):
        self.assertEqual(
            intersect_windows((at(1), at(2)), [(at(1), None)]), [(at(1), at(2))]
        )

    def test_nothing_in_common(self):
        self.assertEqual(intersect_windows((at(1), at(2)), [(at(5), None)]), [])

    def test_touching_windows_have_nothing_in_common(self):
        self.assertEqual(intersect_windows((at(1), at(2)), [(at(2), at(3))]), [])


class RideClockTests(SimpleTestCase):
    def test_now_is_local_wall_clock_labelled_as_utc(self):
        """
        Strava liefert `start_date_local`: 10 Uhr vor Ort steht als 10:00Z in der DB.
        "Jetzt" muss auf dieselbe Skala, sonst liegt ein Tausch um 11 Uhr im Sommer scheinbar
        vor der Fahrt um 10 Uhr (echte UTC-Zeit: 09:00).
        """
        real_now = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
        with patch("django.utils.timezone.now", return_value=real_now):
            self.assertEqual(ride_clock_now(), datetime(2026, 7, 1, 11, 0, tzinfo=UTC))

    def test_winter_offset_differs(self):
        real_now = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        with patch("django.utils.timezone.now", return_value=real_now):
            self.assertEqual(ride_clock_now(), datetime(2026, 1, 1, 10, 0, tzinfo=UTC))

    def test_past_day_means_from_that_morning(self):
        day = date(2026, 1, 1)
        self.assertEqual(moment_for(day), start_of_day(day))

    def test_today_and_no_day_mean_now(self):
        now = ride_clock_now()
        self.assertGreaterEqual(moment_for(None), now)
        self.assertGreaterEqual(moment_for(now.date()), now)

    def test_date_in_windows_follows_the_same_half_open_rule(self):
        self.assertTrue(date_in_windows([(at(1), at(2))], at(1)))
        self.assertFalse(date_in_windows([(at(1), at(2))], at(2)))
        self.assertTrue(date_in_windows([(at(1), None)], at(9)))


class BackfillConversionTests(SimpleTestCase):
    """Die Umrechnung aus Migration 0025: Datum + alter Tachostand → Zeitpunkt."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.migration = import_module(
            "app_maintenance.migrations.0025_backfill_usage_periods"
        )

    def setUp(self):
        self.rides = [
            (datetime(2026, 6, 1, 10, tzinfo=UTC), 10.0),
            (datetime(2026, 6, 2, 8, tzinfo=UTC), 20.0),
            (datetime(2026, 6, 2, 18, tzinfo=UTC), 30.0),
            (datetime(2026, 6, 3, 10, tzinfo=UTC), 40.0),
        ]

    def _km(self, start, end=None):
        return RideLedger(self.rides).km_between(start, end)

    def test_same_day_rides_in_the_snapshot_stay_before(self):
        """Tausch am 2.6. bei km 30: die Morgenfahrt zählt noch zum alten Teil."""
        moment = self.migration._moment(date(2026, 6, 2), 30.0, self.rides)
        self.assertEqual(self._km(None, moment), 30.0)
        self.assertEqual(self._km(moment, None), 70.0)

    def test_snapshot_after_all_rides_of_the_day(self):
        """So entstand der Stand beim Rückdatieren: alle Fahrten des Tages eingeschlossen."""
        moment = self.migration._moment(date(2026, 6, 2), 60.0, self.rides)
        self.assertEqual(self._km(moment, None), 40.0)

    def test_without_snapshot_the_day_counts_from_the_morning(self):
        moment = self.migration._moment(date(2026, 6, 2), None, self.rides)
        self.assertEqual(self._km(moment, None), 90.0)

    def test_stale_snapshot_loses_against_the_date(self):
        """
        Ältere Fahrten wurden nach dem Tausch importiert, der alte Stand ist zu klein. Das
        Datum hat der Nutzer genannt, also gilt es; vorher zählten die falschen Fahrten.
        """
        moment = self.migration._moment(date(2026, 6, 3), 5.0, self.rides)
        self.assertEqual(moment, datetime(2026, 6, 3, tzinfo=UTC))

    def test_overlapping_periods_are_merged(self):
        """Der Bestand kennt Überlappungen (gemessen: 19.691 km an einem Rad mit 9.868 km)."""
        a, b, c = at(1), at(2), at(3)
        self.assertEqual(self.migration._merge([(a, c), (b, None)]), [(a, None)])
        self.assertEqual(self.migration._merge([(b, c), (a, b)]), [(a, c)])
        self.assertEqual(
            self.migration._merge([(a, at(1, 18)), (b, c)]), [(a, at(1, 18)), (b, c)]
        )

    def test_only_visibly_matching_wheels_get_paired(self):
        same_set = self.migration._same_set

        def wheel(name):
            return SimpleNamespace(name=name)

        self.assertTrue(same_set(wheel(""), wheel("")))
        self.assertTrue(same_set(wheel("Carbon vorne"), wheel("Carbon hinten")))
        self.assertFalse(same_set(wheel("Carbon vorne"), wheel("")))
        self.assertFalse(same_set(wheel("Carbon"), wheel("Alu")))

    def test_timedelta_is_only_a_nudge(self):
        moment = self.migration._moment(date(2026, 6, 2), 30.0, self.rides)
        self.assertLess(
            moment - datetime(2026, 6, 2, 8, tzinfo=UTC), timedelta(minutes=1)
        )
