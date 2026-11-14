"""
Gemeinsamer Testaufbau für alles, was mit Kilometern zu tun hat.

Kilometer hängen an der Reihenfolge von Fahrten und Wechseln, also braucht jeder Test
eine eigene Zeitachse: `ride()` und `advance()` schieben die Uhr vor, und "jetzt" im
Code (`ride_clock_now`, `timezone.now`) folgt ihr. So liest sich ein Test wie der Ablauf,
den er prüft.
"""

from datetime import date, timedelta
from itertools import count
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from app_auth.models import StravaProfile
from app_dashboard.models import Ride

from .api.serializers import compute_wear
from .api.usage import ride_clock_now
from .models import Bike, BikeType, Component

_ride_ids = count(70_000_000)
_athlete_ids = count(500_000)


class MaintenanceTestCase(APITestCase):
    bike_type = BikeType.ROAD

    def setUp(self):
        self._now = timezone.now()
        patcher = patch("django.utils.timezone.now", side_effect=lambda: self._now)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.profile = self.make_profile()
        self.user = self.profile.user
        self.login(self.profile)
        self.bike = self.make_bike()

    def make_profile(self) -> StravaProfile:
        athlete_id = next(_athlete_ids)
        user = get_user_model().objects.create_user(username=f"u{athlete_id}")
        return StravaProfile.objects.create(
            user=user,
            strava_athlete_id=athlete_id,
            access_token="t",
            refresh_token="r",
            expires_at=0,
        )

    def make_bike(self, profile: StravaProfile | None = None) -> Bike:
        return Bike.objects.create(
            athlete=profile or self.profile,
            name="Testrad",
            bike_type=self.bike_type,
        )

    def login(self, profile: StravaProfile) -> None:
        self.client.force_login(profile.user)
        session = self.client.session
        session["strava_athlete_id"] = profile.strava_athlete_id
        session.save()

    def advance(self, **delta) -> None:
        self._now += timedelta(**delta)

    def today(self) -> date:
        return ride_clock_now().date()

    def days_ago(self, days: int) -> date:
        return self.today() - timedelta(days=days)

    def ride(self, km: float, days_ago: int | None = None, weather=None, bike=None):
        """
        Eine Fahrt. Ohne `days_ago` jetzt, und die Uhr läuft danach weiter — ein
        nachfolgender Wechsel liegt also hinter der Fahrt. Mit `days_ago` eine Fahrt in
        der Vergangenheit, wie sie ein später Strava-Sync nachliefert.
        """
        if days_ago is None:
            self.advance(hours=1)
            start = ride_clock_now()
            self.advance(hours=1)
        else:
            start = ride_clock_now() - timedelta(days=days_ago)
        return Ride.objects.create(
            strava_id=next(_ride_ids),
            name="Fahrt",
            distance=km * 1000,
            start_date=start,
            athlete=self.profile,
            bike=bike or self.bike,
            weather_data=weather,
        )

    def wear_km(self, component: Component) -> float:
        return compute_wear(Component.objects.get(pk=component.pk))["wear_km"]
