"""
Migration `0027`: ungruppierte Alt-Plätze gehen im Platz ihres Bereichs auf.

Nachgestellt ist der Stand aus Produktion: "Bremse hinten" mit den blauen Belägen im
Regal, daneben ein ungruppierter Platz desselben Templates mit dem Yellow King, der am
Carbon-Laufrad gekoppelt ist. Die Kopplungs-Auswahl zeigte zweimal "Bremsbeläge Felge
hinten", und die blauen Beläge fanden kein Ziel zum Umziehen.
"""

from importlib import import_module

from django.apps import apps as django_apps

from app_maintenance.models import (
    AssemblyStatus,
    BikeAssembly,
    Component,
    ComponentGroup,
    ComponentSlot,
    ComponentStatus,
    ComponentTemplate,
)
from app_maintenance.testing import MaintenanceTestCase

migration = import_module("app_maintenance.migrations.0027_merge_ungrouped_area_twins")


def run_migration() -> None:
    migration.merge_ungrouped_area_twins(django_apps, None)


class MergeUngroupedAreaTwinsTests(MaintenanceTestCase):
    def setUp(self):
        super().setUp()
        self.pad = ComponentTemplate.objects.get(name="Bremsbeläge Felge hinten")
        self.brake = BikeAssembly.objects.create(
            bike=self.bike,
            group=ComponentGroup.objects.get(name="Bremse hinten"),
            status=AssemblyStatus.ACTIVE,
        )
        self.area_slot = ComponentSlot.objects.create(
            bike=self.bike, assembly=self.brake, template=self.pad
        )
        self.blue = Component.objects.create(
            slot=self.area_slot, brand="SwissStop", status=ComponentStatus.SPARE
        )
        self.loose_slot = ComponentSlot.objects.create(
            bike=self.bike, assembly=None, template=self.pad
        )
        self.yellow = Component.objects.create(
            slot=self.loose_slot, brand="Yellow King", status=ComponentStatus.SPARE
        )

    def test_parts_move_into_the_area_slot(self):
        run_migration()

        self.assertFalse(ComponentSlot.objects.filter(pk=self.loose_slot.pk).exists())
        self.assertEqual(
            set(self.area_slot.components.values_list("pk", flat=True)),
            {self.blue.pk, self.yellow.pk},
        )

    def test_assemblies_endpoint_shows_one_pad_slot(self):
        run_migration()

        res = self.client.get(f"/api/maintenance/bikes/{self.bike.id}/assemblies/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["ungrouped_slots"], [])
        pad_slots = [
            slot
            for assembly in res.data["assemblies"]
            for slot in assembly["slots"]
            if slot["template"] == self.pad.pk
        ]
        self.assertEqual(len(pad_slots), 1)

    def test_link_to_a_wheel_survives(self):
        wheel = BikeAssembly.objects.create(
            bike=self.bike,
            group=ComponentGroup.objects.get(name="Laufrad hinten"),
            status=AssemblyStatus.ACTIVE,
        )
        wheel.linked_components.add(self.yellow)

        run_migration()

        self.assertEqual(list(wheel.linked_components.all()), [self.yellow])

    def test_both_mounted_is_left_alone(self):
        Component.objects.filter(pk__in=[self.blue.pk, self.yellow.pk]).update(
            status=ComponentStatus.MOUNTED
        )

        run_migration()

        self.assertTrue(ComponentSlot.objects.filter(pk=self.loose_slot.pk).exists())
        self.assertEqual(Component.objects.get(pk=self.yellow.pk).slot, self.loose_slot)

    def test_no_area_twin_keeps_the_slot(self):
        chain = ComponentTemplate.objects.get(name="Kette")
        loose_chain = ComponentSlot.objects.create(
            bike=self.bike, assembly=None, template=chain
        )

        run_migration()

        self.assertTrue(ComponentSlot.objects.filter(pk=loose_chain.pk).exists())
