from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase
from rest_framework import status

from app_maintenance.api.serializers import compute_wear
from app_maintenance.api.usage import start_of_day
from app_maintenance.models import (
    AssemblyStatus,
    Bike,
    BikeAssembly,
    BikeType,
    Component,
    ComponentCategory,
    ComponentGroup,
    ComponentSlot,
    ComponentStatus,
    ComponentTemplate,
    GroupKind,
    MaintenanceInterval,
)
from app_maintenance.testing import MaintenanceTestCase


class AssemblyTestBase(MaintenanceTestCase):
    bike_type = BikeType.GRAVEL

    def setUp(self):
        super().setUp()
        self.ride(100, days_ago=10)  # Grundfahrt, liegt vor allem anderen

        # Ein Laufradsatz ist eine echte Baugruppe: nur dafür ergibt activate einen
        # Sinn und nur dafür gibt es mehrere Instanzen (siehe models.py::GroupKind).
        self.wheel_group = ComponentGroup.objects.create(
            name="Laufrad vorne",
            category=ComponentCategory.WHEELS,
            sort_order=30,
            kind=GroupKind.ASSEMBLY,
        )
        self.tire = ComponentTemplate.objects.create(
            name="Reifen vorne",
            category=ComponentCategory.WHEELS,
            group=self.wheel_group,
            warn_km=5000,
        )
        self.rim = ComponentTemplate.objects.create(
            name="Felge vorne",
            category=ComponentCategory.WHEELS,
            group=self.wheel_group,
            warn_km=20000,
        )
        self.sealant = ComponentTemplate.objects.create(
            name="Tubeless Dichtmilch vorne",
            category=ComponentCategory.WHEELS,
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
            group=self.susp_group,
        )

        # Fremdes Template ausserhalb jeder Gruppe.
        self.chain = ComponentTemplate.objects.create(
            name="Kette", category=ComponentCategory.DRIVETRAIN
        )

    def _create_payload(self, **overrides):
        payload = {
            "group_id": self.wheel_group.id,
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
            Component.objects.filter(slot__assembly=assembly).count(),
            2,
        )
        interval = MaintenanceInterval.objects.get(assembly=assembly)
        self.assertEqual(interval.label, "Tubeless Dichtmilch vorne")
        self.assertEqual(interval.interval_days, 120)
        self.assertEqual(interval.km_since_done(), 0.0)

        # Die Grundfahrt liegt vor dem Einbau und zählt nicht.
        comp = Component.objects.filter(slot__assembly=assembly).first()
        self.assertEqual(self.wear_km(comp), 0.0)

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
            Component.objects.filter(slot__assembly=old).exists(),
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
            bike=self.bike, group=self.wheel_group, status=AssemblyStatus.ACTIVE
        )
        with self.assertRaises(ValidationError):
            BikeAssembly(
                bike=self.bike,
                group=self.wheel_group,
                status=AssemblyStatus.ACTIVE,
            ).save()


class AssemblyStatusFieldTests(AssemblyTestBase):
    """
    Der Zustand steckt in einem Feld (`status`), nicht mehr in der Kombination
    `is_active` + `retired_at`. Die drei Properties lesen daraus, und die
    Invariante "höchstens eine aufgezogene Instanz je (bike, group)" hängt
    nicht mehr allein an `clean()`, sondern an einem partiellen
    Unique-Constraint in der DB.
    """

    def test_properties_derive_from_status(self):
        assembly = BikeAssembly.objects.create(
            bike=self.bike, group=self.wheel_group, status=AssemblyStatus.ACTIVE
        )
        self.assertEqual(
            (assembly.is_active, assembly.is_parked, assembly.is_retired),
            (True, False, False),
        )

        assembly.status = AssemblyStatus.PARKED
        self.assertEqual(
            (assembly.is_active, assembly.is_parked, assembly.is_retired),
            (False, True, False),
        )

        assembly.status = AssemblyStatus.RETIRED
        self.assertEqual(
            (assembly.is_active, assembly.is_parked, assembly.is_retired),
            (False, False, True),
        )

    def test_database_rejects_a_second_active_instance(self):
        """
        Der Constraint muss auch greifen, wenn `clean()` gar nicht läuft — genau
        das konnte die alte reine `clean()`-Prüfung nicht (zwei parallele
        Requests, Admin-Bulk-Update, Datenmigration).
        """
        BikeAssembly.objects.create(
            bike=self.bike, group=self.wheel_group, status=AssemblyStatus.ACTIVE
        )
        parked = BikeAssembly.objects.create(
            bike=self.bike, group=self.wheel_group, status=AssemblyStatus.PARKED
        )
        with self.assertRaises(IntegrityError):
            # .update() umgeht save()/full_clean() vollständig.
            BikeAssembly.objects.filter(pk=parked.pk).update(
                status=AssemblyStatus.ACTIVE
            )

    def test_several_parked_and_retired_instances_are_allowed(self):
        """Der Constraint ist partiell — nur `active` ist limitiert."""
        BikeAssembly.objects.create(
            bike=self.bike, group=self.wheel_group, status=AssemblyStatus.ACTIVE
        )
        for _ in range(2):
            BikeAssembly.objects.create(
                bike=self.bike, group=self.wheel_group, status=AssemblyStatus.PARKED
            )
            BikeAssembly.objects.create(
                bike=self.bike,
                group=self.wheel_group,
                status=AssemblyStatus.RETIRED,
                retired_at=date.today(),
            )
        self.assertEqual(
            BikeAssembly.objects.filter(bike=self.bike, group=self.wheel_group).count(),
            5,
        )

    def test_status_is_not_writable_through_patch(self):
        """
        Ein Zustandswechsel muss über activate/retire/restore laufen: per PATCH
        gesetzt bliebe die AssemblyUsagePeriod offen und der abgezogene Satz
        sammelte weiter km.
        """
        assembly = BikeAssembly.objects.create(
            bike=self.bike, group=self.wheel_group, status=AssemblyStatus.ACTIVE
        )
        res = self.client.patch(
            f"/api/maintenance/assemblies/{assembly.id}/",
            {"status": AssemblyStatus.PARKED, "name": "Umbenannt"},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        assembly.refresh_from_db()
        self.assertEqual(assembly.status, AssemblyStatus.ACTIVE)
        self.assertEqual(assembly.name, "Umbenannt")

    def test_serializer_still_exposes_the_derived_flags(self):
        """Das Frontend-Model setzt `is_active`/`is_parked` weiterhin voraus."""
        assembly = BikeAssembly.objects.create(
            bike=self.bike, group=self.wheel_group, status=AssemblyStatus.PARKED
        )
        res = self.client.get(f"/api/maintenance/assemblies/{assembly.id}/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["status"], AssemblyStatus.PARKED)
        self.assertFalse(res.data["is_active"])
        self.assertTrue(res.data["is_parked"])


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
        Ein abgenommenes Teil taucht als Übernahme-Vorschlag in `spare_components` auf,
        unabhängig davon, ob seine ursprüngliche Baugruppe noch aktiv ist.
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
        self.client.post(f"/api/maintenance/components/{rim.id}/unmount/")

        res = self.client.get(f"/api/maintenance/bikes/{self.bike.id}/assemblies/")
        spares = {s["template"]: s for s in res.data["spare_components"]}
        self.assertIn(self.rim.id, spares)
        self.assertEqual(spares[self.rim.id]["id"], rim.id)

    def test_spare_components_lists_every_candidate_for_a_template(self):
        """
        Regressionstest: mit mehreren abgenommenen Teilen desselben Templates
        darf `spare_components` nicht auf einen einzigen Kandidaten
        zusammenfallen (raten, welcher "der richtige" ist, schlägt bei
        mehrdeutigen Altdaten regelmäßig fehl) — der Client soll stattdessen
        aus allen wählen können.
        """
        # Zwei getrennte (ausgemusterte) Baugruppen-Instanzen, damit beide
        # Kandidaten je einen eigenen Slot fürs Template bekommen können
        # (uniq_assembly_template erlaubt nur einen Slot je Baugruppe+Template).
        spares = []
        for brand in ("Mavic", "Devox"):
            assembly = BikeAssembly.objects.create(
                bike=self.bike,
                group=self.wheel_group,
                status=AssemblyStatus.RETIRED,
                retired_at=date.today(),
            )
            spares.append(
                Component.objects.create(
                    slot=ComponentSlot.objects.create(
                        bike=self.bike, assembly=assembly, template=self.rim
                    ),
                    brand=brand,
                    status=ComponentStatus.SPARE,
                )
            )

        res = self.client.get(f"/api/maintenance/bikes/{self.bike.id}/assemblies/")
        rim_spare_ids = {
            s["id"]
            for s in res.data["spare_components"]
            if s["template"] == self.rim.id
        }
        self.assertEqual(rim_spare_ids, {c.id for c in spares})

    def test_assembly_km_counts_only_time_on_the_bike(self):
        """
        `assembly_km` misst die Nutzung der Baugruppe selbst (Nutzungsperioden),
        nicht den Einbau des ältesten Teils — sonst würde ein einzelner Reifenwechsel
        die Laufleistung des Laufradsatzes zurücksetzen.
        """
        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/",
            self._create_payload(installed_at=str(self.days_ago(5))),
            format="json",
        )
        assembly_id = res.data["id"]
        # Die Grundfahrt lag vor dem Einbau → assembly_km == 0.
        detail = self.client.get(f"/api/maintenance/assemblies/{assembly_id}/")
        self.assertEqual(detail.data["assembly_km"], 0.0)

        self.ride(60)
        detail = self.client.get(f"/api/maintenance/assemblies/{assembly_id}/")
        self.assertEqual(detail.data["assembly_km"], 60.0)

        # Ein neuer Reifen setzt die Laufleistung des Laufrads nicht zurück.
        slot = ComponentSlot.objects.get(assembly_id=assembly_id, template=self.tire)
        self.client.post(
            f"/api/maintenance/slots/{slot.id}/components/",
            {"brand": "Neu"},
            format="json",
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
        self.assertEqual(Component.objects.filter(slot__assembly=summer).count(), 2)
        # Geparkt = abgeschlossene Periode, aufgezogen = offene Periode.
        self.assertIsNone(summer.open_period())
        self.assertIsNotNone(winter.open_period())

    def test_switching_back_and_forth_keeps_one_active(self):
        summer_id = self._create(name="Sommer-LRS")
        winter_id = self._create(name="Winter-LRS")

        self.advance(hours=1)
        self.client.post(f"/api/maintenance/assemblies/{winter_id}/activate/")
        self.advance(hours=1)
        self.client.post(f"/api/maintenance/assemblies/{summer_id}/activate/")

        self.assertEqual(
            BikeAssembly.objects.filter(
                bike=self.bike, group=self.wheel_group, status=AssemblyStatus.ACTIVE
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

    def test_retire_retires_components_and_closes_their_periods(self):
        summer_id = self._create(name="Sommer-LRS")
        res = self.client.post(f"/api/maintenance/assemblies/{summer_id}/retire/")
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)

        summer = BikeAssembly.objects.get(pk=summer_id)
        self.assertIsNotNone(summer.retired_at)
        self.assertFalse(summer.is_parked)
        for comp in Component.objects.filter(slot__assembly=summer):
            self.assertTrue(comp.is_retired)
            self.assertFalse(comp.periods.filter(ended_at__isnull=True).exists())

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
        self.login(self.make_profile())

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
        self.ride(50)  # 50 km auf dem Sommer-LRS

        summer_comp = Component.objects.filter(slot__assembly_id=summer_id).first()
        self.assertEqual(self.wear_km(summer_comp), 50.0)

        # Winter-LRS aufziehen -> Sommer-LRS wird geparkt.
        winter_id = self._create("Winter-LRS")
        self.client.post(f"/api/maintenance/assemblies/{winter_id}/activate/")

        # 30 km auf dem Winter-LRS.
        self.ride(30)

        winter_comp = Component.objects.filter(slot__assembly_id=winter_id).first()

        self.assertEqual(self.bike.total_distance_km, 180.0)  # 100 + 50 + 30
        self.assertEqual(self.wear_km(summer_comp), 50.0)
        self.assertEqual(self.wear_km(winter_comp), 30.0)

    def test_parked_assembly_still_ages_in_days(self):
        summer_id = self._create("Sommer-LRS")
        summer_comp = Component.objects.filter(slot__assembly_id=summer_id).first()
        summer_comp.installed_at = date.today() - timedelta(days=40)
        summer_comp.save()

        winter_id = self._create("Winter-LRS")
        self.client.post(f"/api/maintenance/assemblies/{winter_id}/activate/")

        wear = compute_wear(summer_comp)
        self.assertEqual(
            wear["wear_days"],
            40,
            "Gummi altert auch im Keller — die Tage-Achse friert nicht ein.",
        )

    def test_parked_assembly_is_excluded_from_bike_overview(self):
        self._create("Sommer-LRS")
        winter_id = self._create("Winter-LRS")
        self.client.post(f"/api/maintenance/assemblies/{winter_id}/activate/")

        res = self.client.get(f"/api/maintenance/bikes/{self.bike.id}/")
        slot_ids = {s["id"] for s in res.data["slots"]}
        parked_slot_ids = set(
            ComponentSlot.objects.filter(
                assembly__bike=self.bike, assembly__status=AssemblyStatus.PARKED
            ).values_list("id", flat=True)
        )
        self.assertTrue(parked_slot_ids)
        self.assertFalse(
            slot_ids & parked_slot_ids,
            "Slots geparkter Baugruppen duerfen nicht in der Bike-Uebersicht auftauchen.",
        )


class AssemblyOutsideTheApiTests(AssemblyTestBase):
    """
    Baugruppen und Teile, die außerhalb der API entstehen (Admin, seed_dev_data), halten
    die Invariante selbst: `save()` öffnet den Zeitraum, sonst sammelte das Teil nie km.
    """

    def _direct_assembly(self, name):
        assembly = BikeAssembly.objects.create(
            bike=self.bike,
            group=self.wheel_group,
            name=name,
            installed_at=date.today() - timedelta(days=30),
        )
        slot = ComponentSlot.objects.create(
            bike=self.bike, assembly=assembly, template=self.tire
        )
        component = Component.objects.create(
            slot=slot, installed_at=date.today() - timedelta(days=30)
        )
        return assembly, component

    def test_periods_open_by_themselves_and_count_from_the_install_date(self):
        assembly, component = self._direct_assembly("Alt-LRS")
        self.assertEqual(assembly.periods.count(), 1)
        self.assertEqual(self.wear_km(component), 100.0)
        self.assertEqual(assembly.compute_km(), 100.0)

    def test_parking_freezes_its_km(self):
        assembly, component = self._direct_assembly("Alt-LRS")

        payload = self._create_payload(name="Neu-LRS")
        payload["activate"] = True
        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/", payload, format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)

        self.ride(60)
        assembly.refresh_from_db()
        self.assertTrue(assembly.is_parked)
        self.assertEqual(assembly.compute_km(), 100.0)
        self.assertEqual(self.wear_km(component), 100.0)


class AssemblyReuseExistingComponentTests(AssemblyTestBase):
    """
    "Vorhandene Komponente übernehmen": beim Anlegen einer Baugruppe kann ein
    Part-Item statt brand/model eine `existing_slot_id` mitgeben — den bislang
    ungruppierten Slot samt montierter Komponente, der dann in die neue
    Baugruppe umgehängt wird, statt eine neue Component anzulegen.
    """

    def _ungrouped_slot(self, template, installed_at, status=ComponentStatus.MOUNTED):
        slot = ComponentSlot.objects.create(bike=self.bike, template=template)
        Component.objects.create(
            slot=slot,
            brand="Alt-Marke",
            model_name="Alt-Modell",
            installed_at=installed_at,
            status=status,
        )
        return slot

    def test_reuse_moves_slot_instead_of_creating_new_component(self):
        old_slot = self._ungrouped_slot(self.tire, date.today() - timedelta(days=10))
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
        # Der Reifen ist seit 30 Tagen drauf und hat die Grundfahrt (100 km) mitgemacht,
        # bevor er einer Gruppe zugeordnet wird.
        old_slot = self._ungrouped_slot(self.tire, date.today() - timedelta(days=30))

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
        self.assertEqual(
            self.wear_km(tire_component),
            100.0,
            "Die Nutzungsperiode darf den Verlauf des übernommenen Teils nicht abschneiden.",
        )

        # Die Baugruppe ist am Rad, seit das übernommene Teil es ist.
        assembly = BikeAssembly.objects.get(pk=res.data["id"])
        self.assertEqual(assembly.compute_km(), 100.0)

        # Die neu angelegte Felge zählt dagegen erst ab jetzt.
        rim_component = ComponentSlot.objects.get(
            assembly=assembly, template=self.rim
        ).mounted_component
        self.assertEqual(self.wear_km(rim_component), 0.0)

    def test_reuse_rejects_slot_of_another_bike(self):
        other_bike = Bike.objects.create(
            athlete=self.profile,
            strava_bike_id="asm2",
            name="Zweitrad",
            bike_type=BikeType.GRAVEL,
        )
        other_slot = ComponentSlot.objects.create(bike=other_bike, template=self.tire)
        Component.objects.create(slot=other_slot, installed_at=date.today())

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
        rim_slot = self._ungrouped_slot(self.rim, date.today())

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
            self.tire, date.today(), status=ComponentStatus.SPARE
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


class AssemblyReuseSpareComponentTests(AssemblyTestBase):
    """
    Verwandter Fall zu `AssemblyReuseExistingComponentTests`: `reuse_component_id`
    holt ein Teil aus dem Regal, statt es neu anlegen zu müssen — unabhängig davon, ob
    dessen alte Baugruppe noch existiert/aktiv ist (Regressionsfall für den
    Prod-Bug: eine ausgemusterte Mavic-Felge tauchte nirgends mehr als
    Übernahme-Vorschlag auf).
    """

    def _spare_component(self, template, installed_at, ridden=True):
        # Wie im Prod-Fall: das Teil hängt an einer inzwischen ausgemusterten
        # Baugruppe, nicht an einem ungruppierten Slot (der wäre wegen
        # `uniq_bike_template_ungrouped` je Template nur einmal möglich).
        old_assembly = BikeAssembly.objects.create(
            bike=self.bike,
            group=self.wheel_group,
            status=AssemblyStatus.RETIRED,
            retired_at=date.today() - timedelta(days=1),
        )
        slot = ComponentSlot.objects.create(
            bike=self.bike, assembly=old_assembly, template=template
        )
        component = Component.objects.create(
            slot=slot,
            brand="Mavic",
            model_name="Cosmic CX80",
            installed_at=installed_at,
            status=ComponentStatus.SPARE,
        )
        if ridden:
            component.periods.create(
                started_at=start_of_day(installed_at),
                ended_at=start_of_day(date.today() - timedelta(days=1)),
            )
        return component

    def test_reuse_remounts_component_into_a_fresh_slot(self):
        spare = self._spare_component(self.rim, date.today() - timedelta(days=20))

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
        self.assertEqual(spare.slot.assembly_id, res.data["id"])
        # Kein zweites Component-Objekt angelegt.
        self.assertEqual(
            Component.objects.filter(brand="Mavic", model_name="Cosmic CX80").count(),
            1,
        )

    def test_reuse_keeps_prior_wear_and_days_aging(self):
        """
        Die km hängen an den Zeiträumen des Teils, nicht am Platz: das Teil hat die
        Grundfahrt (100 km) mitgemacht und nimmt sie mit. Die Standzeit dazwischen zählt
        nicht, die Tage-Achse altert seit dem ursprünglichen Einbau weiter.
        """
        spare = self._spare_component(self.rim, date.today() - timedelta(days=20))

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
        self.assertEqual(self.wear_km(spare), 100.0)
        self.assertEqual(
            compute_wear(spare)["wear_days"],
            20,
            "Die Tage-Achse zählt seit dem ursprünglichen Einbau weiter.",
        )

        # Weitere 30 km zählen oben drauf.
        self.ride(30)
        self.assertEqual(self.wear_km(spare), 130.0)

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
        spare = self._spare_component(self.tire, date.today(), ridden=False)

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
            slot=other_slot, status=ComponentStatus.SPARE
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
        Component.objects.create(slot=old_slot, installed_at=date.today())
        spare = self._spare_component(self.rim, date.today(), ridden=False)

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
    Hinterrad-Laufradsatz wechseln statt (wie zuvor) unter "Antrieb" zu hängen —
    ohne diese Zuordnung würde `activate/` auf "Laufrad hinten" die Kassette nicht
    mit erfassen.
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
