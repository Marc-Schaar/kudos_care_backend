"""
Tests rund um die Frage, was eine Baugruppe eigentlich ist.

Drei Änderungen, die zusammengehören:

* **Vorne/hinten ist ein Feld** (`MountPosition`), kein Wort im Namen. Vorher
  las das Frontend-Diagramm die Seite per String-Match aus `display_name` —
  Umbenennen brach das, und die Kassette trug "hinten" nie im Namen.
* **`GroupKind`** trennt echte Baugruppen (Laufradsatz: am Stück wechselbar)
  von Bereichen (Bremse, Cockpit: Teile verschleißen unabhängig). `activate`
  und `swap` gibt es nur noch für erstere.
* **Nicht angehakte Teile bleiben beim Erneuern am Rad** statt ersatzlos zu
  verschwinden, und **Löschen löst nur die Gruppierung auf**, statt Teile samt
  Historie mitzunehmen.
"""

from django.test import TestCase
from rest_framework import status

from app_maintenance.models import (
    AssemblyStatus,
    BikeAssembly,
    Component,
    ComponentCategory,
    ComponentGroup,
    ComponentSlot,
    ComponentTemplate,
    GroupKind,
    MaintenanceKind,
)
from app_maintenance.test_assemblies import AssemblyTestBase


class AssemblySwapKeepsUncheckedPartsTests(AssemblyTestBase):
    """
    Beim Teile-Erneuern bleiben die **nicht** angehakten Teile am Rad.

    Vorher fielen sie ersatzlos weg: die alte Instanz wurde komplett
    ausgemustert und die neue nur aus den angehakten Zeilen aufgebaut. Wer an
    einem Antrieb nur die Kette erneuerte, verlor Kurbel und Tretlager.
    """

    def setUp(self):
        super().setUp()
        self.rim = ComponentTemplate.objects.create(
            name="Felge vorne",
            category=ComponentCategory.WHEELS,
            is_system=False,
            group=self.wheel_group,
            warn_km=20000,
            maintenance_kind=MaintenanceKind.PART,
        )

    def _create_with_both(self) -> int:
        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/",
            {
                "group_id": self.wheel_group.id,
                "parts": [
                    {
                        "template_id": self.tire.id,
                        "include": True,
                        "brand": "Alt-Reifen",
                    },
                    {"template_id": self.rim.id, "include": True, "brand": "Alt-Felge"},
                ],
                "intervals": [],
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        return res.data["id"]

    def _swap_renewing_only_the_tire(self, assembly_id: int):
        return self.client.post(
            f"/api/maintenance/assemblies/{assembly_id}/swap/",
            {
                "parts": [
                    {
                        "template_id": self.tire.id,
                        "include": True,
                        "brand": "Neu-Reifen",
                    },
                    {"template_id": self.rim.id, "include": False},
                ],
                "intervals": [],
            },
            format="json",
        )

    def test_unchecked_part_stays_mounted_with_its_history(self):
        assembly_id = self._create_with_both()
        rim_before = Component.objects.get(slot__template=self.rim, is_mounted=True)

        res = self._swap_renewing_only_the_tire(assembly_id)
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        new_id = res.data["id"]

        mounted = Component.objects.filter(slot__bike=self.bike, is_mounted=True)
        self.assertEqual(
            sorted(c.slot.template.name for c in mounted),
            ["Felge vorne", "Reifen vorne"],
            "Das nicht angehakte Teil muss am Rad bleiben.",
        )

        # Dieselbe Component wie vorher, nicht eine neu angelegte Kopie.
        rim_after = Component.objects.get(slot__template=self.rim, is_mounted=True)
        self.assertEqual(rim_after.id, rim_before.id)
        self.assertEqual(rim_after.brand, "Alt-Felge")
        self.assertEqual(rim_after.installed_at, rim_before.installed_at)
        rim_after.slot.refresh_from_db()
        self.assertEqual(rim_after.slot.assembly_id, new_id)

    def test_checked_part_is_renewed_and_the_old_one_retired(self):
        assembly_id = self._create_with_both()
        self._swap_renewing_only_the_tire(assembly_id)

        tire_mounted = Component.objects.get(slot__template=self.tire, is_mounted=True)
        self.assertEqual(tire_mounted.brand, "Neu-Reifen")
        self.assertTrue(
            Component.objects.filter(
                slot__template=self.tire, is_mounted=False, brand="Alt-Reifen"
            ).exists()
        )


class GroupKindTests(AssemblyTestBase):
    """
    `activate`/`swap` gibt es nur für echte Baugruppen. Ein Bereich bündelt
    Teile, die denselben Ort teilen, aber unabhängig verschleißen — dort wird
    einzeln getauscht.
    """

    def setUp(self):
        super().setUp()
        self.area = ComponentGroup.objects.create(
            name="Cockpit-Test",
            category=ComponentCategory.COCKPIT,
            sort_order=50,
            kind=GroupKind.AREA,
        )
        self.saddle = ComponentTemplate.objects.create(
            name="Sattel",
            category=ComponentCategory.COCKPIT,
            is_system=False,
            group=self.area,
            warn_km=20000,
            maintenance_kind=MaintenanceKind.PART,
        )
        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/",
            {
                "group_id": self.area.id,
                "parts": [{"template_id": self.saddle.id, "include": True}],
                "intervals": [],
            },
            format="json",
        )
        self.area_assembly_id = res.data["id"]

    def test_area_cannot_be_swapped(self):
        res = self.client.post(
            f"/api/maintenance/assemblies/{self.area_assembly_id}/swap/",
            {
                "parts": [{"template_id": self.saddle.id, "include": True}],
                "intervals": [],
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data["code"], "not_an_assembly")

    def test_area_cannot_be_activated(self):
        assembly = BikeAssembly.objects.get(pk=self.area_assembly_id)
        assembly.status = AssemblyStatus.PARKED
        assembly.save()
        res = self.client.post(f"/api/maintenance/assemblies/{assembly.id}/activate/")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data["code"], "not_an_assembly")

    def test_real_assembly_can_still_be_swapped(self):
        created = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/",
            {
                "group_id": self.wheel_group.id,
                "parts": [{"template_id": self.tire.id, "include": True}],
                "intervals": [],
            },
            format="json",
        )
        swap = self.client.post(
            f"/api/maintenance/assemblies/{created.data['id']}/swap/",
            {
                "parts": [{"template_id": self.tire.id, "include": True}],
                "intervals": [],
            },
            format="json",
        )
        self.assertEqual(swap.status_code, status.HTTP_201_CREATED)


class AssemblyDeleteDissolvesTests(AssemblyTestBase):
    """
    Löschen löst nur die Gruppierung auf — die Teile bleiben am Rad.

    Vorher cascadierte das Löschen über Slots, Components, Intervalle und
    Nutzungszeiträume: die Gruppierung wegzuwerfen hiess, die Kette samt ihrer
    Verschleiss-Historie wegzuwerfen.
    """

    def _create(self) -> int:
        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/",
            {
                "group_id": self.wheel_group.id,
                "parts": [
                    {"template_id": self.tire.id, "include": True, "brand": "Conti"}
                ],
                "intervals": [],
            },
            format="json",
        )
        return res.data["id"]

    def test_delete_keeps_the_components_and_ungroups_them(self):
        assembly_id = self._create()
        component = Component.objects.get(slot__bike=self.bike, is_mounted=True)

        res = self.client.delete(f"/api/maintenance/assemblies/{assembly_id}/")
        self.assertEqual(res.status_code, status.HTTP_204_NO_CONTENT)

        self.assertFalse(BikeAssembly.objects.filter(pk=assembly_id).exists())
        component.refresh_from_db()
        self.assertTrue(component.is_mounted, "Das Teil muss montiert bleiben.")
        self.assertEqual(component.brand, "Conti")
        component.slot.refresh_from_db()
        self.assertIsNone(component.slot.assembly_id, "Slot ist jetzt ungruppiert.")

    def test_dissolved_slots_show_up_as_ungrouped(self):
        assembly_id = self._create()
        self.client.delete(f"/api/maintenance/assemblies/{assembly_id}/")

        res = self.client.get(f"/api/maintenance/bikes/{self.bike.id}/assemblies/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res.data["assemblies"]), 0)
        names = [s["display_name"] for s in res.data["ungrouped_slots"]]
        self.assertIn("Reifen vorne", names)

    def test_conflict_with_an_existing_ungrouped_slot_is_refused(self):
        """
        Je (bike, template) darf es nur einen Slot ohne Baugruppe geben. Statt in
        einen IntegrityError zu laufen, wird der Fall vorher erkannt.
        """
        assembly_id = self._create()
        ComponentSlot.objects.create(bike=self.bike, template=self.tire, assembly=None)

        res = self.client.delete(f"/api/maintenance/assemblies/{assembly_id}/")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(BikeAssembly.objects.filter(pk=assembly_id).exists())


class MountPositionTests(TestCase):
    """
    Regressionstest gegen die Katalog-Daten aus Migration `0023`: die Seite
    steht im Feld, nicht im Namen.
    """

    def test_cassette_is_pinned_to_the_rear(self):
        """Der Anlass: die Kassette trug "hinten" nie im Namen."""
        cassette = ComponentTemplate.objects.get(name="Kassette")
        self.assertEqual(cassette.position, "rear")
        self.assertNotIn("hinten", cassette.name.lower())

    def test_wheel_templates_carry_their_side(self):
        self.assertEqual(
            ComponentTemplate.objects.get(name="Reifen vorne").position, "front"
        )
        self.assertEqual(
            ComponentTemplate.objects.get(name="Reifen hinten").position, "rear"
        )

    def test_wheel_groups_carry_their_side(self):
        self.assertEqual(
            ComponentGroup.objects.get(name="Laufrad vorne").position, "front"
        )
        self.assertEqual(
            ComponentGroup.objects.get(name="Laufrad hinten").position, "rear"
        )

    def test_only_wheelsets_are_real_assemblies(self):
        assemblies = set(
            ComponentGroup.objects.filter(kind=GroupKind.ASSEMBLY).values_list(
                "name", flat=True
            )
        )
        self.assertEqual(assemblies, {"Laufrad vorne", "Laufrad hinten"})
        self.assertEqual(
            ComponentGroup.objects.get(name="Cockpit").kind, GroupKind.AREA
        )
        self.assertEqual(
            ComponentGroup.objects.get(name="Antrieb").kind, GroupKind.AREA
        )
