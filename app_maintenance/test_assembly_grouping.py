"""
Tests rund um die Frage, was eine Baugruppe eigentlich ist.

* **Vorne/hinten ist ein Feld** (`MountPosition`), kein Wort im Namen. Vorher
  las das Frontend-Diagramm die Seite per String-Match aus `display_name` —
  Umbenennen brach das, und die Kassette trug "hinten" nie im Namen.
* **`GroupKind`** trennt echte Baugruppen (Laufrad: am Stück wechselbar)
  von Bereichen (Bremse, Cockpit: Teile verschleißen unabhängig). Wechseln gibt es nur
  für erstere; siehe auch `test_part_lifecycle.py::AreaHasOneInstanceTests`.
"""

from django.test import TestCase
from rest_framework import status

from app_maintenance.models import (
    AssemblyStatus,
    BikeAssembly,
    ComponentCategory,
    ComponentGroup,
    ComponentTemplate,
    GroupKind,
    MaintenanceKind,
)
from app_maintenance.test_assemblies import AssemblyTestBase


class GroupKindTests(AssemblyTestBase):
    """
    Wechseln gibt es nur für echte Baugruppen. Ein Bereich bündelt Teile, die denselben
    Ort teilen, aber unabhängig verschleißen — dort wird einzeln getauscht.
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

    def test_switching_between_two_areas_is_refused(self):
        """Zwei Bereiche derselben Gruppe gibt es nur noch als Altlast."""
        other = BikeAssembly.objects.create(
            bike=self.bike, group=self.area, status=AssemblyStatus.PARKED
        )
        res = self.client.post(f"/api/maintenance/assemblies/{other.id}/activate/")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data["code"], "not_an_assembly")

    def test_an_area_without_an_active_instance_can_go_back_on(self):
        assembly = BikeAssembly.objects.get(pk=self.area_assembly_id)
        assembly.status = AssemblyStatus.PARKED
        assembly.save()
        res = self.client.post(f"/api/maintenance/assemblies/{assembly.id}/activate/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        assembly.refresh_from_db()
        self.assertEqual(assembly.status, AssemblyStatus.ACTIVE)


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
