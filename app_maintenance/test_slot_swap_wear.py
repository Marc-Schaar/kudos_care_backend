"""
Verschleiß beim Einzeltausch im Slot — zwei Teile, die sich einen Slot teilen.

Anlass sind zwei Bremsbelag-Sätze am selben Rad (Carbon- und Alu-Mischung, die
zum Laufradsatz passen muss). Beim Durchmessen kamen zwei Fehler heraus, beide
unabhängig von den Baugruppen:

* **Ausbauen hielt den km-Stand nicht fest.** `SlotMountView`/`SlotUnmountView`
  setzten nur `retired_at`. Damit griff in `api/usage.py` der Altbestands-
  Fallback ("ausgebaut, aber ohne erfassten km-Stand → bis heute rechnen"), der
  für Daten von vor Migration `0018` gedacht war — ein nach 100 km ausgebauter
  Belag stand bei 150 km, nachdem der Nachfolger 50 km gefahren war, und wuchs
  weiter.
* **Wiedermontieren zählte die Standzeit mit.** Eine Komponente hat genau einen
  Einbaupunkt; die Pause, in der das andere Teil im Slot sass, war nirgends
  erfasst. Der zurückgeholte Belag stand bei 180 statt 130 km.
"""

from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from app_auth.models import StravaProfile
from app_dashboard.models import Ride
from app_maintenance.api.serializers import compute_wear
from app_maintenance.models import (
    AssemblyUsagePeriod,
    Bike,
    BikeAssembly,
    BikeType,
    Component,
    ComponentCategory,
    ComponentGroup,
    ComponentSlot,
    ComponentTemplate,
    GroupKind,
    MaintenanceKind,
)


class SlotSwapWearTests(APITestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(username="pads")
        self.profile = StravaProfile.objects.create(
            user=user,
            strava_athlete_id=4242,
            access_token="t",
            refresh_token="r",
            expires_at=0,
        )
        self.client.force_login(user)
        session = self.client.session
        session["strava_athlete_id"] = 4242
        session.save()

        self.bike = Bike.objects.create(
            athlete=self.profile,
            strava_bike_id="pads1",
            name="Felt",
            bike_type=BikeType.ROAD,
        )
        group = ComponentGroup.objects.create(
            name="Bremse hinten",
            category=ComponentCategory.BRAKES,
            kind=GroupKind.AREA,
        )
        assembly = BikeAssembly.objects.create(
            bike=self.bike, group=group, installed_at=date(2025, 1, 1)
        )
        AssemblyUsagePeriod.objects.create(
            assembly=assembly, started_at=date(2025, 1, 1), started_distance_km=0
        )
        template = ComponentTemplate.objects.create(
            name="Bremsbeläge Felge hinten",
            category=ComponentCategory.BRAKES,
            group=group,
            warn_km=3000,
            maintenance_kind=MaintenanceKind.PART,
        )
        self.slot = ComponentSlot.objects.create(
            bike=self.bike, assembly=assembly, template=template
        )
        self.blue = Component.objects.create(
            slot=self.slot,
            brand="SwissStop",
            model_name="Die Blauen",
            installed_at=date(2025, 1, 1),
            distance_at_install=0,
            is_mounted=True,
        )
        self._rides = 0

    def _ride(self, km: int):
        self._rides += 1
        Ride.objects.create(
            strava_id=90000 + self._rides,
            name=f"Fahrt {self._rides}",
            distance=km * 1000,
            start_date=timezone.now() - timedelta(days=60 - self._rides),
            athlete=self.profile,
            bike=self.bike,
        )

    def _wear(self, component: Component) -> float | None:
        component.refresh_from_db()
        return compute_wear(component, self.bike.total_distance_km)["wear_km"]

    def _mount_new_yellow(self) -> Component:
        res = self.client.post(
            f"/api/maintenance/slots/{self.slot.id}/components/",
            {
                "brand": "SwissStop",
                "model_name": "Yellow King",
                "installed_at": str(date.today()),
                "distance_at_install": self.bike.total_distance_km,
                "is_mounted": True,
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        return Component.objects.get(slot=self.slot, model_name="Yellow King")

    def test_removed_part_stops_collecting_km(self):
        """Der Kernfall: ein ausgebautes Teil darf nicht weiterzählen."""
        self._ride(100)
        self.assertEqual(self._wear(self.blue), 100.0)

        self._mount_new_yellow()
        self._ride(50)

        self.assertEqual(
            self._wear(self.blue), 100.0, "Ausgebaut heißt: die Zahl steht still."
        )

    def test_newly_mounted_part_starts_at_zero(self):
        self._ride(100)
        yellow = self._mount_new_yellow()
        self._ride(50)
        self.assertEqual(self._wear(yellow), 50.0)

    def test_switching_back_and_forth_keeps_both_numbers_right(self):
        """
        Beide Belagsätze abwechselnd fahren — jeder zählt nur seine eigenen km.
        """
        self._ride(100)  # Blau: 100
        yellow = self._mount_new_yellow()
        self._ride(50)  # Gelb: 50

        res = self.client.post(
            f"/api/maintenance/slots/{self.slot.id}/mount/",
            {"component_id": self.blue.id},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self._ride(30)  # Blau: 100 + 30

        self.assertEqual(self.bike.total_distance_km, 180.0)
        self.assertEqual(self._wear(self.blue), 130.0)
        self.assertEqual(self._wear(yellow), 50.0)

    def test_unmount_endpoint_also_records_the_odometer(self):
        self._ride(100)
        res = self.client.post(f"/api/maintenance/slots/{self.slot.id}/unmount/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        self.blue.refresh_from_db()
        self.assertFalse(self.blue.is_mounted)
        self.assertEqual(self.blue.distance_at_retire, 100.0)

        self._ride(40)
        self.assertEqual(self._wear(self.blue), 100.0)

    def test_carried_over_wear_survives_a_second_round_trip(self):
        """Mehrfaches Hin und Her addiert sich, statt sich zu überschreiben."""
        self._ride(100)
        yellow = self._mount_new_yellow()
        self._ride(50)

        self.client.post(
            f"/api/maintenance/slots/{self.slot.id}/mount/",
            {"component_id": self.blue.id},
            format="json",
        )
        self._ride(30)

        self.client.post(
            f"/api/maintenance/slots/{self.slot.id}/mount/",
            {"component_id": yellow.id},
            format="json",
        )
        self._ride(20)

        # Blau: 100 + 30 = 130, Gelb: 50 + 20 = 70
        self.assertEqual(self._wear(self.blue), 130.0)
        self.assertEqual(self._wear(yellow), 70.0)
