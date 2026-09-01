from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
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

        available = {g["id"]: g for g in res.data["available_groups"]}
        # Bereits aktive Gruppe bleibt anlegbar (zweiter Satz, wird geparkt
        # angelegt) — nur mit einem Hinweis-Flag markiert, nicht ausgeblendet.
        self.assertIn(self.wheel_group.id, available)
        self.assertTrue(available[self.wheel_group.id]["has_active_instance"])
        self.assertNotIn(self.susp_group.id, available)  # falscher Bike-Typ

    def test_spare_components_lists_removed_parts(self):
        """
        Ein ausgebautes (nicht montiertes) Teil taucht als Übernahme-Vorschlag
        in `spare_components` auf, unabhängig davon, ob seine ursprüngliche
        Baugruppe noch existiert/aktiv ist.
        """
        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/",
            self._create_payload(),
            format="json",
        )
        assembly_id = res.data["id"]
        rim = Component.objects.get(
            slot__assembly_id=assembly_id, slot__template=self.rim
        )
        rim.is_mounted = False
        rim.retired_at = date.today()
        rim.save(update_fields=["is_mounted", "retired_at"])

        res = self.client.get(f"/api/maintenance/bikes/{self.bike.id}/assemblies/")
        spares = {s["template"]: s for s in res.data["spare_components"]}
        self.assertIn(self.rim.id, spares)
        self.assertEqual(spares[self.rim.id]["id"], rim.id)

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


class AssemblyReuseExistingComponentTests(AssemblyTestBase):
    """
    "Vorhandene Komponente übernehmen": beim Anlegen einer Baugruppe kann ein
    Part-Item statt brand/model eine `existing_slot_id` mitgeben — den bislang
    ungruppierten Slot samt montierter Komponente, der dann in die neue
    Baugruppe umgehängt wird, statt eine neue Component anzulegen.
    """

    def _ungrouped_slot(
        self, template, installed_at, distance_at_install, is_mounted=True
    ):
        slot = ComponentSlot.objects.create(bike=self.bike, template=template)
        Component.objects.create(
            slot=slot,
            brand="Alt-Marke",
            model_name="Alt-Modell",
            installed_at=installed_at,
            distance_at_install=distance_at_install,
            is_mounted=is_mounted,
        )
        return slot

    def test_reuse_moves_slot_instead_of_creating_new_component(self):
        old_slot = self._ungrouped_slot(
            self.tire, date.today() - timedelta(days=10), 50.0
        )
        old_component_id = old_slot.mounted_component.id

        payload = self._create_payload()
        payload["parts"][0] = {
            "template_id": self.tire.id,
            "include": True,
            "existing_slot_id": old_slot.id,
        }
        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/", payload, format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)

        old_slot.refresh_from_db()
        self.assertEqual(old_slot.assembly_id, res.data["id"])
        # Dieselbe Component wiederverwendet, keine zweite angelegt.
        self.assertEqual(
            list(Component.objects.filter(slot=old_slot).values_list("id", flat=True)),
            [old_component_id],
        )
        # Die Felge (kein existing_slot_id) läuft normal weiter über eine neue Component.
        rim_slot = ComponentSlot.objects.get(
            assembly_id=res.data["id"], template=self.rim
        )
        self.assertEqual(rim_slot.mounted_component.brand, "DT Swiss")

    def test_reuse_preserves_wear_history_instead_of_resetting_it(self):
        # 100 km Grundfahrt (aus AssemblyTestBase) — Reifen seit 30 Tagen/20 km
        # montiert, also bereits 80 km gefahren, bevor er einer Gruppe zugeordnet wird.
        old_slot = self._ungrouped_slot(
            self.tire, date.today() - timedelta(days=30), 20.0
        )

        payload = self._create_payload()
        payload["parts"][0] = {
            "template_id": self.tire.id,
            "include": True,
            "existing_slot_id": old_slot.id,
        }
        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/", payload, format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)

        tire_component = old_slot.mounted_component
        tire_component.refresh_from_db()
        self.assertEqual(
            compute_wear(tire_component, self.bike.total_distance_km)["wear_km"],
            80.0,
            "Die Nutzungsperiode darf den Verlauf des übernommenen Teils nicht abschneiden.",
        )

        assembly = BikeAssembly.objects.get(pk=res.data["id"])
        period = assembly.open_period()
        self.assertEqual(period.started_at, date.today() - timedelta(days=30))
        self.assertEqual(period.started_distance_km, 20.0)

        # Die neu angelegte Felge zählt dagegen erst ab heute/dem aktuellen km-Stand.
        rim_component = ComponentSlot.objects.get(
            assembly=assembly, template=self.rim
        ).mounted_component
        self.assertEqual(
            compute_wear(rim_component, self.bike.total_distance_km)["wear_km"], 0.0
        )

    def test_reuse_rejects_slot_of_another_bike(self):
        other_bike = Bike.objects.create(
            athlete=self.profile,
            strava_bike_id="asm2",
            name="Zweitrad",
            bike_type=BikeType.GRAVEL,
        )
        other_slot = ComponentSlot.objects.create(bike=other_bike, template=self.tire)
        Component.objects.create(
            slot=other_slot,
            installed_at=date.today(),
            distance_at_install=0.0,
            is_mounted=True,
        )

        payload = self._create_payload()
        payload["parts"][0] = {
            "template_id": self.tire.id,
            "include": True,
            "existing_slot_id": other_slot.id,
        }
        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/", payload, format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reuse_rejects_already_grouped_slot(self):
        first = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/",
            self._create_payload(),
            format="json",
        )
        grouped_tire_slot = ComponentSlot.objects.get(
            assembly_id=first.data["id"], template=self.tire
        )

        payload = self._create_payload(name="Zweiter Satz")
        payload["parts"][0] = {
            "template_id": self.tire.id,
            "include": True,
            "existing_slot_id": grouped_tire_slot.id,
        }
        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/", payload, format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reuse_rejects_template_mismatch(self):
        rim_slot = self._ungrouped_slot(self.rim, date.today(), 0.0)

        payload = self._create_payload()
        # existing_slot_id auf der TIRE-Zeile, aber der Slot ist für die Felge.
        payload["parts"][0] = {
            "template_id": self.tire.id,
            "include": True,
            "existing_slot_id": rim_slot.id,
        }
        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/", payload, format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reuse_rejects_slot_without_mounted_component(self):
        empty_slot = self._ungrouped_slot(
            self.tire, date.today(), 0.0, is_mounted=False
        )

        payload = self._create_payload()
        payload["parts"][0] = {
            "template_id": self.tire.id,
            "include": True,
            "existing_slot_id": empty_slot.id,
        }
        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/", payload, format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_swap_also_supports_reusing_an_existing_slot(self):
        """Derselbe Code-Pfad (_build_assembly_from_request) trägt auch swap/."""
        first = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/",
            self._create_payload(),
            format="json",
        )
        spare_slot = self._ungrouped_slot(
            self.tire, date.today() - timedelta(days=5), 90.0
        )

        res = self.client.post(
            f"/api/maintenance/assemblies/{first.data['id']}/swap/",
            {
                "parts": [
                    {
                        "template_id": self.tire.id,
                        "include": True,
                        "existing_slot_id": spare_slot.id,
                    },
                    {"template_id": self.rim.id, "include": True, "brand": "Newmen"},
                ],
                "intervals": [],
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        spare_slot.refresh_from_db()
        self.assertEqual(spare_slot.assembly_id, res.data["id"])


class AssemblyReuseSpareComponentTests(AssemblyTestBase):
    """
    Verwandter Fall zu `AssemblyReuseExistingComponentTests`: `reuse_component_id`
    reaktiviert ein bereits *ausgebautes* Teil (z.B. ein zurückgelegter
    Laufradsatz-Teil), statt es neu anlegen zu müssen — unabhängig davon, ob
    dessen alte Baugruppe noch existiert/aktiv ist (Regressionsfall für den
    Prod-Bug: eine ausgemusterte Mavic-Felge tauchte nirgends mehr als
    Übernahme-Vorschlag auf).
    """

    def _spare_component(self, template, installed_at, distance_at_install):
        # Wie im Prod-Fall: das Teil hängt an einer inzwischen ausgemusterten
        # Baugruppe, nicht an einem ungruppierten Slot (der wäre wegen
        # `uniq_bike_template_ungrouped` je Template nur einmal möglich).
        old_assembly = BikeAssembly.objects.create(
            bike=self.bike,
            group=self.wheel_group,
            is_active=False,
            retired_at=date.today() - timedelta(days=1),
        )
        slot = ComponentSlot.objects.create(
            bike=self.bike, assembly=old_assembly, template=template
        )
        return Component.objects.create(
            slot=slot,
            brand="Mavic",
            model_name="Cosmic CX80",
            installed_at=installed_at,
            distance_at_install=distance_at_install,
            is_mounted=False,
            retired_at=date.today() - timedelta(days=1),
            distance_at_retire=90.0,
        )

    def test_reuse_remounts_component_into_a_fresh_slot(self):
        spare = self._spare_component(
            self.rim, date.today() - timedelta(days=20), 10.0
        )

        payload = self._create_payload()
        payload["parts"][1] = {
            "template_id": self.rim.id,
            "include": True,
            "reuse_component_id": spare.id,
        }
        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/", payload, format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)

        spare.refresh_from_db()
        self.assertTrue(spare.is_mounted)
        self.assertIsNone(spare.retired_at)
        self.assertIsNone(spare.distance_at_retire)
        self.assertEqual(spare.slot.assembly_id, res.data["id"])
        # Kein zweites Component-Objekt angelegt.
        self.assertEqual(
            Component.objects.filter(brand="Mavic", model_name="Cosmic CX80").count(),
            1,
        )

    def test_reuse_resets_km_axis_but_keeps_days_aging(self):
        """
        Anders als bei `existing_slot_id` (durchgehender Einbau) war das Teil
        hier wirklich ausgebaut — die km-Achse muss nach der Wiedermontage bei
        0 anfangen (sonst zählten km mit, die zwischenzeitlich ein anderer
        Laufradsatz gefahren ist), die Tage-Achse aber altert seit dem
        ursprünglichen Einbau unverändert weiter (genau wie bei einer
        geparkten statt ausgebauten Baugruppe).
        """
        spare = self._spare_component(
            self.rim, date.today() - timedelta(days=20), 10.0
        )

        payload = self._create_payload()
        payload["parts"][1] = {
            "template_id": self.rim.id,
            "include": True,
            "reuse_component_id": spare.id,
        }
        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/", payload, format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)

        spare.refresh_from_db()
        self.assertEqual(
            compute_wear(spare, self.bike.total_distance_km)["wear_km"],
            0.0,
            "Die km-Achse startet nach der Wiedermontage neu bei 0.",
        )
        self.assertEqual(
            compute_wear(spare, self.bike.total_distance_km)["wear_days"],
            20,
            "Die Tage-Achse zählt seit dem ursprünglichen Einbau weiter.",
        )

        assembly = BikeAssembly.objects.get(pk=res.data["id"])
        period = assembly.open_period()
        self.assertEqual(period.started_at, date.today())
        self.assertEqual(period.started_distance_km, self.bike.total_distance_km)

    def test_reuse_rejects_still_mounted_component(self):
        first = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/",
            self._create_payload(),
            format="json",
        )
        mounted_rim = ComponentSlot.objects.get(
            assembly_id=first.data["id"], template=self.rim
        ).mounted_component

        payload = self._create_payload(name="Zweiter Satz")
        payload["parts"][1] = {
            "template_id": self.rim.id,
            "include": True,
            "reuse_component_id": mounted_rim.id,
        }
        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/", payload, format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reuse_rejects_template_mismatch(self):
        spare = self._spare_component(self.tire, date.today(), 0.0)

        payload = self._create_payload()
        # reuse_component_id auf der FELGE-Zeile, aber die Component ist ein Reifen.
        payload["parts"][1] = {
            "template_id": self.rim.id,
            "include": True,
            "reuse_component_id": spare.id,
        }
        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/", payload, format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reuse_rejects_component_of_another_bike(self):
        other_bike = Bike.objects.create(
            athlete=self.profile,
            strava_bike_id="asm3",
            name="Drittrad",
            bike_type=BikeType.GRAVEL,
        )
        other_slot = ComponentSlot.objects.create(bike=other_bike, template=self.rim)
        other_spare = Component.objects.create(
            slot=other_slot,
            installed_at=date.today(),
            distance_at_install=0.0,
            is_mounted=False,
            retired_at=date.today(),
        )

        payload = self._create_payload()
        payload["parts"][1] = {
            "template_id": self.rim.id,
            "include": True,
            "reuse_component_id": other_spare.id,
        }
        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/", payload, format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_existing_slot_id_and_reuse_component_id_are_mutually_exclusive(self):
        old_slot = ComponentSlot.objects.create(bike=self.bike, template=self.rim)
        Component.objects.create(
            slot=old_slot,
            installed_at=date.today(),
            distance_at_install=0.0,
            is_mounted=True,
        )
        spare = self._spare_component(self.rim, date.today(), 0.0)

        payload = self._create_payload()
        payload["parts"][1] = {
            "template_id": self.rim.id,
            "include": True,
            "existing_slot_id": old_slot.id,
            "reuse_component_id": spare.id,
        }
        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/", payload, format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)


class CassetteBelongsToRearWheelGroupTests(TestCase):
    """
    Regressionstest für die Katalog-Migration 0020: Kassette soll mit dem
    Hinterrad-Laufradsatz tauschbar sein statt (wie zuvor) unter "Antrieb" zu
    hängen — ohne diese Zuordnung würde `assemblies/<id>/swap/` bzw.
    `activate/` auf "Laufrad hinten" die Kassette nicht mit erfassen.
    """

    def test_kassette_template_is_in_laufrad_hinten_group(self):
        group = ComponentGroup.objects.get(name="Laufrad hinten")
        self.assertTrue(
            group.templates.filter(name="Kassette").exists(),
            "Kassette fehlt in der Baugruppe 'Laufrad hinten' — siehe Migration 0020.",
        )
        self.assertFalse(
            ComponentGroup.objects.get(name="Antrieb")
            .templates.filter(name="Kassette")
            .exists()
        )
        self.assertEqual(
            ComponentTemplate.objects.get(name="Kassette").maintenance_kind, "part"
        )
