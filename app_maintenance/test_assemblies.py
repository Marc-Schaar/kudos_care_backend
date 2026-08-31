from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from app_auth.models import StravaProfile
from app_dashboard.models import Ride
from app_maintenance.models import (
    Bike,
    BikeAssembly,
    BikeType,
    Component,
    ComponentCategory,
    ComponentGroup,
    ComponentSlot,
    ComponentTemplate,
    MaintenanceInterval,
)


def _make_profile(user, strava_athlete_id=24680):
    return StravaProfile.objects.create(
        user=user,
        strava_athlete_id=strava_athlete_id,
        access_token="token",
        refresh_token="refresh",
        expires_at=0,
    )


class AssemblyTestBase(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="asm", password="pw")
        self.profile = _make_profile(self.user)
        self.client.force_login(self.user)
        session = self.client.session
        session["strava_athlete_id"] = self.profile.strava_athlete_id
        session.save()

        self.bike = Bike.objects.create(
            athlete=self.profile,
            strava_bike_id="asm1",
            name="Testrad",
            bike_type=BikeType.GRAVEL,
        )
        Ride.objects.create(
            strava_id=5001,
            name="Grundfahrt",
            distance=100_000,  # 100 km → bike.total_distance_km == 100.0
            start_date=timezone.now() - timedelta(days=10),
            athlete=self.profile,
            bike=self.bike,
        )

        self.wheel_group = ComponentGroup.objects.create(
            name="Laufrad vorne", category=ComponentCategory.WHEELS, sort_order=30
        )
        self.tire = ComponentTemplate.objects.create(
            name="Reifen vorne",
            category=ComponentCategory.WHEELS,
            is_system=False,
            group=self.wheel_group,
            warn_km=5000,
        )
        self.rim = ComponentTemplate.objects.create(
            name="Felge vorne",
            category=ComponentCategory.WHEELS,
            is_system=False,
            group=self.wheel_group,
            warn_km=20000,
        )
        self.sealant = ComponentTemplate.objects.create(
            name="Tubeless Dichtmilch vorne",
            category=ComponentCategory.WHEELS,
            is_system=False,
            group=self.wheel_group,
            warn_days=120,
            maintenance_kind="consumable",
        )

        # Nur MTB — zum Testen der Bike-Typ-Ablehnung.
        self.susp_group = ComponentGroup.objects.create(
            name="Federung",
            category=ComponentCategory.SUSPENSION,
            applicable_bike_types=["mtb", "ebike_mtb"],
        )
        self.fork = ComponentTemplate.objects.create(
            name="Gabelöl",
            category=ComponentCategory.SUSPENSION,
            is_system=False,
            group=self.susp_group,
        )

        # Fremdes Template ausserhalb jeder Gruppe.
        self.chain = ComponentTemplate.objects.create(
            name="Kette", category=ComponentCategory.DRIVETRAIN, is_system=False
        )

    def _create_payload(self, **overrides):
        payload = {
            "group_id": self.wheel_group.id,
            "installed_at": str(date.today()),
            "parts": [
                {"template_id": self.tire.id, "include": True, "brand": "Schwalbe"},
                {"template_id": self.rim.id, "include": True, "brand": "DT Swiss"},
            ],
            "intervals": [
                {"template_id": self.sealant.id, "include": True},
            ],
        }
        payload.update(overrides)
        return payload


class AssemblyCreateTests(AssemblyTestBase):
    def test_create_builds_slots_components_and_intervals(self):
        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/",
            self._create_payload(),
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)

        assembly = BikeAssembly.objects.get(bike=self.bike, group=self.wheel_group)
        self.assertTrue(assembly.is_active)
        self.assertEqual(assembly.slots.count(), 2)
        self.assertEqual(
            Component.objects.filter(slot__assembly=assembly, is_mounted=True).count(),
            2,
        )
        interval = MaintenanceInterval.objects.get(assembly=assembly)
        self.assertEqual(interval.label, "Tubeless Dichtmilch vorne")
        self.assertEqual(interval.interval_days, 120)
        self.assertEqual(interval.last_done_distance_km, 100.0)

        # Component-km-Baseline = aktueller Bike-Stand.
        comp = Component.objects.filter(slot__assembly=assembly).first()
        self.assertEqual(comp.distance_at_install, 100.0)

    def test_create_skips_non_included_items(self):
        payload = self._create_payload()
        payload["parts"][1]["include"] = False
        payload["intervals"][0]["include"] = False
        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/", payload, format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        assembly = BikeAssembly.objects.get(bike=self.bike, group=self.wheel_group)
        self.assertEqual(assembly.slots.count(), 1)
        self.assertEqual(
            MaintenanceInterval.objects.filter(assembly=assembly).count(), 0
        )

    def test_create_rejects_template_not_in_group(self):
        payload = self._create_payload()
        payload["parts"].append({"template_id": self.chain.id, "include": True})
        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/", payload, format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(BikeAssembly.objects.filter(bike=self.bike).exists())

    def test_create_rejects_consumable_id_in_parts(self):
        payload = self._create_payload()
        payload["parts"].append({"template_id": self.sealant.id, "include": True})
        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/", payload, format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_rejects_bike_type_mismatch(self):
        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/",
            {"group_id": self.susp_group.id, "parts": [], "intervals": []},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data["code"], "bike_type_mismatch")

    def test_create_conflicts_when_active_assembly_exists(self):
        self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/",
            self._create_payload(),
            format="json",
        )
        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/",
            self._create_payload(),
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(res.data["code"], "already_active")

    def test_auth_required(self):
        self.client.logout()
        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/",
            self._create_payload(),
            format="json",
        )
        self.assertIn(
            res.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )

    def test_model_enforces_single_active_per_group(self):
        BikeAssembly.objects.create(
            bike=self.bike, group=self.wheel_group, is_active=True
        )
        with self.assertRaises(ValidationError):
            BikeAssembly(bike=self.bike, group=self.wheel_group, is_active=True).save()


class AssemblyListTests(AssemblyTestBase):
    def test_list_returns_assemblies_ungrouped_and_available(self):
        self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/",
            self._create_payload(),
            format="json",
        )
        # Ein Alt-Slot ohne Baugruppe.
        ComponentSlot.objects.create(bike=self.bike, template=self.chain)

        res = self.client.get(f"/api/maintenance/bikes/{self.bike.id}/assemblies/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res.data["assemblies"]), 1)
        self.assertEqual(len(res.data["ungrouped_slots"]), 1)

        available_ids = {g["id"] for g in res.data["available_groups"]}
        self.assertIn(self.wheel_group.id, {self.wheel_group.id})  # sanity
        self.assertNotIn(self.wheel_group.id, available_ids)  # bereits aktiv
        self.assertNotIn(self.susp_group.id, available_ids)  # falscher Bike-Typ

    def test_assembly_km_uses_oldest_mounted_component(self):
        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/",
            self._create_payload(installed_at=str(date.today() - timedelta(days=5))),
            format="json",
        )
        assembly_id = res.data["id"]
        # Baseline = 100 km (aktueller Stand beim Einbau) → assembly_km == 0.
        detail = self.client.get(f"/api/maintenance/assemblies/{assembly_id}/")
        self.assertEqual(detail.data["assembly_km"], 0.0)

        # Ein älteres Teil "vorziehen": distance_at_install kleiner setzen.
        comp = Component.objects.filter(slot__assembly_id=assembly_id).first()
        comp.distance_at_install = 40.0
        comp.save()
        detail = self.client.get(f"/api/maintenance/assemblies/{assembly_id}/")
        self.assertEqual(detail.data["assembly_km"], 60.0)

    def test_worst_status_reflects_overdue_interval(self):
        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/",
            self._create_payload(),
            format="json",
        )
        assembly_id = res.data["id"]
        interval = MaintenanceInterval.objects.get(assembly_id=assembly_id)
        interval.last_done_at = date.today() - timedelta(days=400)  # > 120 Tage
        interval.save()

        detail = self.client.get(f"/api/maintenance/assemblies/{assembly_id}/")
        self.assertEqual(detail.data["worst_status"], "critical")


class AssemblySwapTests(AssemblyTestBase):
    def _create(self):
        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/",
            self._create_payload(),
            format="json",
        )
        return res.data["id"]

    def test_swap_deactivates_old_and_creates_new_active(self):
        old_id = self._create()
        old_component_ids = list(
            Component.objects.filter(slot__assembly_id=old_id).values_list(
                "id", flat=True
            )
        )

        res = self.client.post(
            f"/api/maintenance/assemblies/{old_id}/swap/",
            {
                "installed_at": str(date.today()),
                "parts": [
                    {"template_id": self.tire.id, "include": True, "brand": "Conti"},
                    {"template_id": self.rim.id, "include": True, "brand": "Newmen"},
                ],
                "intervals": [{"template_id": self.sealant.id, "include": True}],
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)

        old = BikeAssembly.objects.get(id=old_id)
        self.assertFalse(old.is_active)
        self.assertIsNotNone(old.retired_at)

        new_id = res.data["id"]
        self.assertNotEqual(new_id, old_id)
        self.assertEqual(
            BikeAssembly.objects.filter(
                bike=self.bike, group=self.wheel_group, is_active=True
            ).count(),
            1,
        )

        # Alte Komponenten ausgebaut.
        for cid in old_component_ids:
            self.assertFalse(Component.objects.get(id=cid).is_mounted)

    def test_swap_history_is_queryable(self):
        old_id = self._create()
        self.client.post(
            f"/api/maintenance/assemblies/{old_id}/swap/",
            {
                "parts": [{"template_id": self.tire.id, "include": True}],
                "intervals": [],
            },
            format="json",
        )
        self.assertEqual(
            BikeAssembly.objects.filter(bike=self.bike, group=self.wheel_group).count(),
            2,
        )
        self.assertEqual(
            BikeAssembly.objects.filter(
                bike=self.bike, group=self.wheel_group, is_active=False
            ).count(),
            1,
        )
