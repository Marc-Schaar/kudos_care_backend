from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from app_auth.models import StravaProfile
from app_dashboard.models import Ride
from app_maintenance.api.serializers import compute_wear
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

    def test_second_instance_of_same_group_is_created_parked(self):
        """
        Zweiter Laufradsatz: erlaubt, aber standardmäßig geparkt — die
        aufgezogene Instanz soll nicht ungefragt verdrängt werden.
        """
        first = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/",
            self._create_payload(),
            format="json",
        )
        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/",
            self._create_payload(),
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertFalse(res.data["is_active"])
        self.assertTrue(res.data["is_parked"])
        self.assertTrue(
            BikeAssembly.objects.get(pk=first.data["id"]).is_active,
            "Die bestehende Baugruppe muss aufgezogen bleiben.",
        )

    def test_create_with_activate_parks_the_previous_instance(self):
        first = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/",
            self._create_payload(),
            format="json",
        )
        payload = self._create_payload()
        payload["activate"] = True
        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/",
            payload,
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertTrue(res.data["is_active"])

        old = BikeAssembly.objects.get(pk=first.data["id"])
        self.assertFalse(old.is_active)
        self.assertIsNone(old.retired_at, "Parken darf nicht ausmustern.")
        self.assertTrue(
            Component.objects.filter(slot__assembly=old, is_mounted=True).exists(),
            "Die Teile bleiben auf dem geparkten Satz montiert.",
        )

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

    def test_assembly_km_counts_only_time_on_the_bike(self):
        """
        `assembly_km` misst die Nutzung der Baugruppe selbst (Nutzungsperioden),
        nicht mehr den Einbau-km-Stand des ältesten Teils — sonst würde ein
        einzelner Reifenwechsel die Laufleistung des Laufradsatzes zurücksetzen.
        """
        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/",
            self._create_payload(installed_at=str(date.today() - timedelta(days=5))),
            format="json",
        )
        assembly_id = res.data["id"]
        # Baseline = 100 km (Stand beim Einbau) → assembly_km == 0.
        detail = self.client.get(f"/api/maintenance/assemblies/{assembly_id}/")
        self.assertEqual(detail.data["assembly_km"], 0.0)

        # Ein einzelnes Teil "vorziehen" ändert die Laufleistung der Baugruppe nicht.
        comp = Component.objects.filter(slot__assembly_id=assembly_id).first()
        comp.distance_at_install = 40.0
        comp.save()
        detail = self.client.get(f"/api/maintenance/assemblies/{assembly_id}/")
        self.assertEqual(detail.data["assembly_km"], 0.0)

        # Weitere 60 km fahren → die Baugruppe zählt sie mit.
        Ride.objects.create(
            strava_id=5002,
            name="Ausfahrt",
            distance=60_000,
            start_date=timezone.now(),
            athlete=self.profile,
            bike=self.bike,
        )
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


class AssemblyActivateTests(AssemblyTestBase):
    """
    Wechsel zwischen zwei Baugruppen-Instanzen derselben Gruppe
    (Sommer-/Winter-Laufradsatz).
    """

    def _create(self, name="", activate=None):
        payload = self._create_payload(name=name)
        if activate is not None:
            payload["activate"] = activate
        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/",
            payload,
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        return res.data["id"]

    def test_activate_parks_the_other_instance_without_retiring_it(self):
        summer_id = self._create(name="Sommer-LRS")
        winter_id = self._create(name="Winter-LRS")  # geparkt angelegt

        res = self.client.post(f"/api/maintenance/assemblies/{winter_id}/activate/")
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)

        summer = BikeAssembly.objects.get(pk=summer_id)
        winter = BikeAssembly.objects.get(pk=winter_id)
        self.assertFalse(summer.is_active)
        self.assertIsNone(summer.retired_at)
        self.assertTrue(summer.is_parked)
        self.assertTrue(winter.is_active)

        # Die Teile des geparkten Satzes bleiben auf ihm montiert.
        self.assertEqual(
            Component.objects.filter(slot__assembly=summer, is_mounted=True).count(), 2
        )
        # Geparkt = abgeschlossene Periode, aufgezogen = offene Periode.
        self.assertIsNone(summer.open_period())
        self.assertIsNotNone(winter.open_period())

    def test_switching_back_and_forth_keeps_one_active(self):
        summer_id = self._create(name="Sommer-LRS")
        winter_id = self._create(name="Winter-LRS")

        self.client.post(f"/api/maintenance/assemblies/{winter_id}/activate/")
        self.client.post(f"/api/maintenance/assemblies/{summer_id}/activate/")

        self.assertEqual(
            BikeAssembly.objects.filter(
                bike=self.bike, group=self.wheel_group, is_active=True
            ).count(),
            1,
        )
        self.assertTrue(BikeAssembly.objects.get(pk=summer_id).is_active)
        self.assertEqual(
            BikeAssembly.objects.get(pk=summer_id).periods.count(),
            2,
            "Zweites Aufziehen eroeffnet einen zweiten Nutzungszeitraum.",
        )

    def test_activate_rejects_retired_assembly(self):
        summer_id = self._create(name="Sommer-LRS")
        self.client.post(f"/api/maintenance/assemblies/{summer_id}/retire/")

        res = self.client.post(f"/api/maintenance/assemblies/{summer_id}/activate/")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data["code"], "retired")

    def test_retire_unmounts_components_and_records_odometer(self):
        summer_id = self._create(name="Sommer-LRS")
        res = self.client.post(f"/api/maintenance/assemblies/{summer_id}/retire/")
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)

        summer = BikeAssembly.objects.get(pk=summer_id)
        self.assertIsNotNone(summer.retired_at)
        self.assertFalse(summer.is_parked)
        for comp in Component.objects.filter(slot__assembly=summer):
            self.assertFalse(comp.is_mounted)
            self.assertEqual(comp.distance_at_retire, 100.0)

    def test_list_separates_active_and_parked(self):
        self._create(name="Sommer-LRS")
        self._create(name="Winter-LRS")

        res = self.client.get(f"/api/maintenance/bikes/{self.bike.id}/assemblies/")
        self.assertEqual(len(res.data["assemblies"]), 1)
        self.assertEqual(len(res.data["parked_assemblies"]), 1)
        self.assertEqual(res.data["parked_assemblies"][0]["display_name"], "Winter-LRS")

    def test_retired_assembly_is_not_offered_as_alternative(self):
        self._create(name="Sommer-LRS")
        winter_id = self._create(name="Winter-LRS")
        self.client.post(f"/api/maintenance/assemblies/{winter_id}/retire/")

        res = self.client.get(f"/api/maintenance/bikes/{self.bike.id}/assemblies/")
        self.assertEqual(res.data["parked_assemblies"], [])

    def test_activate_requires_own_athlete(self):
        winter_id = self._create(name="Winter-LRS", activate=False)
        other_user = get_user_model().objects.create_user(
            username="other", password="pw"
        )
        _make_profile(other_user, strava_athlete_id=99999)
        self.client.force_login(other_user)
        session = self.client.session
        session["strava_athlete_id"] = 99999
        session.save()

        res = self.client.post(f"/api/maintenance/assemblies/{winter_id}/activate/")
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

    def test_auth_required(self):
        winter_id = self._create(name="Winter-LRS", activate=False)
        self.client.logout()
        res = self.client.post(f"/api/maintenance/assemblies/{winter_id}/activate/")
        self.assertIn(
            res.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )


class ParkedAssemblyWearTests(AssemblyTestBase):
    """
    Der Kernfall der Funktion: ein abgezogener Laufradsatz darf keine km und
    keinen Wetter-Verschleiss sammeln, waehrend das Bike auf dem anderen Satz
    weiterfaehrt. Die Tage-Achse laeuft dagegen bewusst weiter.
    """

    def _ride(self, strava_id, km, days_ago=0):
        return Ride.objects.create(
            strava_id=strava_id,
            name=f"Fahrt {strava_id}",
            distance=km * 1000,
            start_date=timezone.now() - timedelta(days=days_ago),
            athlete=self.profile,
            bike=self.bike,
        )

    def _create(self, name):
        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/",
            self._create_payload(name=name),
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        return res.data["id"]

    def test_parked_assembly_stops_collecting_km(self):
        summer_id = self._create("Sommer-LRS")
        self._ride(6001, 50)  # 50 km auf dem Sommer-LRS

        summer_comp = Component.objects.filter(slot__assembly_id=summer_id).first()
        self.assertEqual(
            compute_wear(summer_comp, self.bike.total_distance_km)["wear_km"], 50.0
        )

        # Winter-LRS aufziehen -> Sommer-LRS wird geparkt.
        winter_id = self._create("Winter-LRS")
        self.client.post(f"/api/maintenance/assemblies/{winter_id}/activate/")

        # 30 km auf dem Winter-LRS.
        self._ride(6002, 30)

        summer_comp.refresh_from_db()
        winter_comp = Component.objects.filter(slot__assembly_id=winter_id).first()
        bike_km = self.bike.total_distance_km

        self.assertEqual(bike_km, 180.0)  # 100 Grundfahrt + 50 + 30
        self.assertEqual(compute_wear(summer_comp, bike_km)["wear_km"], 50.0)
        self.assertEqual(compute_wear(winter_comp, bike_km)["wear_km"], 30.0)

    def test_parked_assembly_still_ages_in_days(self):
        summer_id = self._create("Sommer-LRS")
        summer_comp = Component.objects.filter(slot__assembly_id=summer_id).first()
        summer_comp.installed_at = date.today() - timedelta(days=40)
        summer_comp.save()

        winter_id = self._create("Winter-LRS")
        self.client.post(f"/api/maintenance/assemblies/{winter_id}/activate/")

        wear = compute_wear(summer_comp, self.bike.total_distance_km)
        self.assertEqual(
            wear["wear_days"],
            40,
            "Gummi altert auch im Keller â€” die Tage-Achse friert nicht ein.",
        )

    def test_parked_assembly_is_excluded_from_bike_overview(self):
        self._create("Sommer-LRS")
        winter_id = self._create("Winter-LRS")
        self.client.post(f"/api/maintenance/assemblies/{winter_id}/activate/")

        res = self.client.get(f"/api/maintenance/bikes/{self.bike.id}/")
        slot_ids = {s["id"] for s in res.data["slots"]}
        parked_slot_ids = set(
            ComponentSlot.objects.filter(
                assembly__bike=self.bike, assembly__is_active=False
            ).values_list("id", flat=True)
        )
        self.assertTrue(parked_slot_ids)
        self.assertFalse(
            slot_ids & parked_slot_ids,
            "Slots geparkter Baugruppen duerfen nicht in der Bike-Uebersicht auftauchen.",
        )


class LegacyAssemblyWithoutPeriodsTests(AssemblyTestBase):
    """
    Baugruppen aus der Zeit vor den Nutzungszeitraeumen â€” oder ausserhalb der API
    angelegt (Admin, seed_dev_data, Datenmigration) â€” haben keine Perioden. Ohne
    Nachziehen beim Abziehen griffe der Alt-Fallback und der geparkte Satz wuerde
    im Keller weiter km sammeln.
    """

    def _legacy_assembly(self, name):
        assembly = BikeAssembly.objects.create(
            bike=self.bike,
            group=self.wheel_group,
            name=name,
            installed_at=date.today() - timedelta(days=30),
        )
        slot = ComponentSlot.objects.create(
            bike=self.bike, assembly=assembly, template=self.tire
        )
        Component.objects.create(
            slot=slot,
            installed_at=date.today() - timedelta(days=30),
            distance_at_install=0.0,
            is_mounted=True,
        )
        return assembly

    def test_parking_a_period_less_assembly_freezes_its_km(self):
        legacy = self._legacy_assembly("Alt-LRS")
        self.assertEqual(legacy.periods.count(), 0)
        self.assertEqual(legacy.compute_km(self.bike.total_distance_km), 100.0)

        # Zweiten Satz anlegen und aufziehen -> der alte wird geparkt.
        payload = self._create_payload(name="Neu-LRS")
        payload["activate"] = True
        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/", payload, format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)

        legacy.refresh_from_db()
        self.assertTrue(legacy.is_parked)
        self.assertEqual(
            legacy.periods.count(),
            1,
            "Beim Abziehen muss rueckwirkend ein Nutzungszeitraum entstehen.",
        )

        # Weitere 60 km auf dem neuen Satz.
        Ride.objects.create(
            strava_id=7001,
            name="Nach dem Wechsel",
            distance=60_000,
            start_date=timezone.now(),
            athlete=self.profile,
            bike=self.bike,
        )
        legacy.refresh_from_db()
        self.assertEqual(
            legacy.compute_km(self.bike.total_distance_km),
            100.0,
            "Der geparkte Alt-Satz darf keine km mehr sammeln.",
        )

    def test_component_wear_of_a_parked_legacy_assembly_freezes_too(self):
        legacy = self._legacy_assembly("Alt-LRS")
        comp = Component.objects.get(slot__assembly=legacy)

        payload = self._create_payload(name="Neu-LRS")
        payload["activate"] = True
        self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/", payload, format="json"
        )
        Ride.objects.create(
            strava_id=7002,
            name="Nach dem Wechsel",
            distance=60_000,
            start_date=timezone.now(),
            athlete=self.profile,
            bike=self.bike,
        )

        comp.refresh_from_db()
        self.assertEqual(
            compute_wear(comp, self.bike.total_distance_km)["wear_km"], 100.0
        )
