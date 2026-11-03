"""
Die Abläufe, um die es in der Teile-Verwaltung geht, von der API aus durchgespielt:

* Ein Laufrad hat mehrere Reifen. Die km zählen immer für den, der gerade drauf ist.
* Ein Bike hat mehrere Laufräder. Beim Wechsel Carbon → Alu wechselt alles mit, was auf
  dem Laufrad sitzt, und dazu die gekoppelten Teile (Bremsbeläge) — nicht die ganze Bremse.
* Ausgemustert wird wahlweise ein einzelnes Teil, ein Laufrad oder der ganze Satz.

Jeder Test hier war vorher entweder falsch oder gar nicht möglich; die Kommentare nennen,
was im alten Stand passierte. Grundlage ist `api/usage.py`: eine Fahrt gehört den Teilen,
die zu ihrem Startzeitpunkt am Rad waren.
"""

from datetime import timedelta
from unittest.mock import patch

from django.db import IntegrityError, transaction

from app_dashboard.api.services import StravaSyncService
from app_maintenance.api.serializers import compute_wear
from app_maintenance.api.services import (
    WeatherWearService,
    get_new_component_warnings,
    ride_wear_breakdown,
)
from app_maintenance.models import (
    AssemblyStatus,
    Bike,
    BikeAssembly,
    Component,
    ComponentGroup,
    ComponentSlot,
    ComponentStatus,
    ComponentTemplate,
    MaintenanceInterval,
)
from app_maintenance.testing import MaintenanceTestCase

P = "/api/maintenance"
#: Wetter mit Regen: bei Reifen und Belägen ein Multiplikator über 1.
RAINY = {
    "precipitation": [6.0, 7.0],
    "temperature_2m": [12.0, 13.0],
    "wind_speed_10m": [10.0, 11.0],
}


class WheelScenario(MaintenanceTestCase):
    """Ein Rennrad mit Laufrädern und Bremse aus dem echten Katalog (Migration 0016)."""

    def setUp(self):
        super().setUp()
        self.front_group = ComponentGroup.objects.get(name="Laufrad vorne")
        self.rear_group = ComponentGroup.objects.get(name="Laufrad hinten")
        self.brake_group = ComponentGroup.objects.get(name="Bremse hinten")
        self.tire_front = ComponentTemplate.objects.get(name="Reifen vorne")
        self.tire_rear = ComponentTemplate.objects.get(name="Reifen hinten")
        self.pad_rear = ComponentTemplate.objects.get(name="Bremsbeläge Felge hinten")
        self.rotor_rear = ComponentTemplate.objects.get(name="Bremsscheibe hinten")

    def post(self, url, body=None, expect=None):
        res = self.client.post(url, body or {}, format="json")
        if expect is not None:
            self.assertEqual(res.status_code, expect, getattr(res, "data", None))
        elif res.status_code >= 400:
            self.fail(f"{url}: {res.status_code} {getattr(res, 'data', None)}")
        return res

    def assembly(self, group, name, template, brand, **extra) -> BikeAssembly:
        res = self.post(
            f"{P}/bikes/{self.bike.id}/assemblies/",
            {
                "group_id": group.id,
                "name": name,
                "parts": [
                    {"template_id": template.id, "include": True, "brand": brand}
                ],
                **extra,
            },
        )
        return BikeAssembly.objects.get(pk=res.data["id"])

    def part_in(self, assembly, template) -> Component:
        return ComponentSlot.objects.get(
            assembly=assembly, template=template
        ).mounted_component

    def slot_of(self, assembly, template) -> ComponentSlot:
        return ComponentSlot.objects.get(assembly=assembly, template=template)

    def add_part(self, slot, brand, **body) -> Component:
        res = self.post(f"{P}/slots/{slot.id}/components/", {"brand": brand, **body})
        return Component.objects.get(pk=res.data["id"])

    def mount(self, component, **body):
        return self.post(f"{P}/components/{component.id}/mount/", body)


class TireSwapTests(WheelScenario):
    """Mehrere Reifen auf einem Laufrad."""

    def setUp(self):
        super().setUp()
        self.wheel = self.assembly(self.front_group, "Alu", self.tire_front, "Conti")
        self.slot = self.slot_of(self.wheel, self.tire_front)
        self.conti = self.slot.mounted_component

    def test_km_always_go_to_the_tire_that_is_on(self):
        self.ride(100)
        pirelli = self.add_part(self.slot, "Pirelli")  # zieht auf, Conti ab

        self.ride(50)
        self.assertEqual(self.wear_km(self.conti), 100)
        self.assertEqual(self.wear_km(pirelli), 50)

        self.mount(self.conti)
        self.ride(30)
        self.assertEqual(self.wear_km(self.conti), 130)
        self.assertEqual(self.wear_km(pirelli), 50)

    def test_every_ride_belongs_to_exactly_one_tire(self):
        pirelli = self.add_part(self.slot, "Pirelli", mount=False)
        total = 0
        for index, km in enumerate((10, 20, 30, 40, 50, 60)):
            self.ride(km)
            total += km
            self.mount(pirelli if index % 2 == 0 else self.conti)
        self.assertEqual(self.wear_km(self.conti) + self.wear_km(pirelli), total)

    def test_new_spare_tire_starts_counting_when_it_goes_on(self):
        """Alt: ein Reifen, der erst ins Regal kam, blieb nach dem Aufziehen ewig ohne km."""
        winter = self.add_part(self.slot, "Winter", mount=False)
        self.assertEqual(winter.status, ComponentStatus.SPARE)
        self.assertIsNone(winter.installed_at)

        self.ride(100)
        self.assertEqual(self.wear_km(winter), 0)

        self.mount(winter)
        winter.refresh_from_db()
        self.assertEqual(winter.installed_at, self.today())
        self.ride(50)
        self.assertEqual(self.wear_km(winter), 50)
        self.assertEqual(self.wear_km(self.conti), 100)

    def test_taken_off_is_spare_and_retired_is_retired(self):
        """Alt: beides hieß "ausgebaut"; gut im Regal und entsorgt ließen sich nicht trennen."""
        winter = self.add_part(self.slot, "Winter")

        self.conti.refresh_from_db()
        self.assertEqual(self.conti.status, ComponentStatus.SPARE)
        self.assertIsNone(self.conti.retired_at)

        self.post(f"{P}/components/{self.conti.id}/retire/")
        self.conti.refresh_from_db()
        self.assertEqual(self.conti.status, ComponentStatus.RETIRED)
        self.assertIsNotNone(self.conti.retired_at)

        res = self.client.get(f"{P}/slots/{self.slot.id}/")
        self.assertEqual(res.data["mounted_component"]["id"], winter.id)
        self.assertEqual(res.data["spare_components"], [])
        self.assertEqual(res.data["retired_components"][0]["id"], self.conti.id)

    def test_retired_part_can_be_restored(self):
        self.post(f"{P}/components/{self.conti.id}/retire/")
        self.post(f"{P}/components/{self.conti.id}/restore/")
        self.conti.refresh_from_db()
        self.assertEqual(self.conti.status, ComponentStatus.SPARE)
        self.assertIsNone(self.conti.retired_at)

    def test_retired_part_stops_collecting(self):
        self.ride(100)
        self.post(f"{P}/components/{self.conti.id}/retire/")
        self.ride(100)
        self.assertEqual(self.wear_km(self.conti), 100)

    def test_unmount_leaves_the_slot_empty_and_keeps_the_part(self):
        self.ride(100)
        self.post(f"{P}/components/{self.conti.id}/unmount/")
        self.ride(100)
        self.assertIsNone(self.slot_of(self.wheel, self.tire_front).mounted_component)
        self.assertEqual(self.wear_km(self.conti), 100)

    def test_status_cannot_be_patched_past_the_bookkeeping(self):
        """Alt: `is_mounted: false` per PATCH, und das Teil sammelte einfach weiter km."""
        res = self.client.patch(
            f"{P}/components/{self.conti.id}/", {"status": "retired"}, format="json"
        )
        self.assertEqual(res.status_code, 200)
        self.conti.refresh_from_db()
        self.assertEqual(self.conti.status, ComponentStatus.MOUNTED)

    def test_age_keeps_running_across_swaps(self):
        """Alt: jedes Wiedermontieren setzte die Tage-Achse auf 0. Gummi altert trotzdem."""
        Component.objects.filter(pk=self.conti.pk).update(
            installed_at=self.days_ago(500)
        )
        self.ride(10)
        winter = self.add_part(self.slot, "Winter")
        self.mount(self.conti)
        self.mount(winter)
        self.mount(self.conti)
        self.conti.refresh_from_db()
        self.assertEqual(self.conti.installed_at, self.days_ago(500))

    def test_weather_wear_is_not_counted_twice_on_repeated_swaps(self):
        """
        Alt: `carried_over_weather_wear_km` addierte beim Wiedermontieren den schon
        enthaltenen Altwert noch einmal — nach zwei Wechseln stand ein Teil bei 200 statt
        100 km. Jetzt zählt die Wetter-Achse dieselben Fahrten wie die km-Achse.
        """
        winter = self.add_part(self.slot, "Winter", mount=False)
        self.ride(100)
        for target in (winter, self.conti, winter, self.conti):
            self.mount(target)
        self.conti.refresh_from_db()
        self.assertEqual(self.conti.weather_wear_km, 100)
        winter.refresh_from_db()
        self.assertEqual(winter.weather_wear_km, 0)

    def test_weather_numbers_are_there_right_after_a_swap(self):
        """Alt: nach dem Tausch stand die Wetter-Achse bis zur nächsten Fahrt auf "unbekannt"."""
        self.ride(100)
        pirelli = self.add_part(self.slot, "Pirelli")
        self.conti.refresh_from_db()
        pirelli.refresh_from_db()
        self.assertEqual(self.conti.weather_wear_km, 100)
        self.assertEqual(pirelli.weather_wear_km, 0)

    def test_rainy_weather_axis_never_lags_behind_the_km_axis(self):
        self.ride(100, weather=RAINY)
        winter = self.add_part(self.slot, "Winter")
        self.ride(40, weather=RAINY)
        self.mount(self.conti)
        for tire in (self.conti, winter):
            tire.refresh_from_db()
            self.assertGreaterEqual(tire.weather_wear_km, self.wear_km(tire))

    def test_prior_km_count_from_the_start(self):
        used = self.add_part(self.slot, "Gebraucht", prior_km=1200)
        self.ride(100)
        self.assertEqual(self.wear_km(used), 1300)
        used.refresh_from_db()
        self.assertEqual(used.weather_wear_km, 1200)

        WeatherWearService.recompute_component(used)
        used.refresh_from_db()
        self.assertEqual(used.weather_wear_km, 1300)

        self.client.patch(
            f"{P}/components/{used.id}/", {"prior_km": 200}, format="json"
        )
        self.assertEqual(self.wear_km(used), 300)
        used.refresh_from_db()
        self.assertEqual(used.weather_wear_km, 300)

    # ── Datumsangaben ────────────────────────────────────────────────────────

    def test_swap_on_a_past_day_counts_from_that_morning(self):
        """ "Den Reifen habe ich schon vorgestern gewechselt." """
        self.client.patch(
            f"{P}/components/{self.conti.id}/",
            {"installed_at": str(self.days_ago(30))},
            format="json",
        )
        self.ride(100, days_ago=5)
        self.ride(50, days_ago=1)
        pirelli = self.add_part(
            self.slot, "Pirelli", installed_at=str(self.days_ago(2))
        )
        self.assertEqual(self.wear_km(pirelli), 50)
        self.assertEqual(self.wear_km(self.conti), 100)

    def test_swap_before_the_current_tire_went_on_is_refused(self):
        res = self.post(
            f"{P}/slots/{self.slot.id}/components/",
            {"brand": "Pirelli", "installed_at": str(self.days_ago(3))},
            expect=400,
        )
        self.assertEqual(res.data["code"], "date_before_mount")
        # Nichts halb angelegt: der Conti ist weiter drauf, kein zweiter Reifen da.
        self.assertEqual(Component.objects.filter(slot=self.slot).count(), 1)
        self.conti.refresh_from_db()
        self.assertTrue(self.conti.is_mounted)

    def test_future_date_is_refused(self):
        winter = self.add_part(self.slot, "Winter", mount=False)
        res = self.post(
            f"{P}/components/{winter.id}/mount/",
            {"on": str(self.today() + timedelta(days=2))},
            expect=400,
        )
        self.assertEqual(res.data["code"], "future_date")

    def test_correcting_the_install_date_moves_where_the_km_start(self):
        """Alt: nur das Datum sprang, die km zählten weiter ab dem alten km-Stand."""
        self.ride(100, days_ago=20)
        self.ride(100, days_ago=2)
        self.assertEqual(self.wear_km(self.conti), 0)

        self.client.patch(
            f"{P}/components/{self.conti.id}/",
            {"installed_at": str(self.days_ago(30))},
            format="json",
        )
        self.assertEqual(self.wear_km(self.conti), 200)

        self.client.patch(
            f"{P}/components/{self.conti.id}/",
            {"installed_at": str(self.days_ago(10))},
            format="json",
        )
        self.assertEqual(self.wear_km(self.conti), 100)


class LateRideTests(WheelScenario):
    """Strava liefert Fahrten nach: was zählt, ist wann gefahren wurde, nicht wann importiert."""

    def setUp(self):
        super().setUp()
        self.wheel = self.assembly(self.front_group, "Alu", self.tire_front, "Conti")
        self.slot = self.slot_of(self.wheel, self.tire_front)
        self.conti = self.slot.mounted_component

    def _backdate_install(self, component, days):
        self.client.patch(
            f"{P}/components/{component.id}/",
            {"installed_at": str(self.days_ago(days))},
            format="json",
        )

    def test_ride_synced_after_the_swap_goes_to_the_old_tire(self):
        """
        Alt der häufigste Fehler überhaupt: Sonntag fahren, abends Reifen tauschen, Montag
        synchronisieren — und die Sonntagsfahrt landete beim neuen Reifen, weil die km am
        Tachostand zum Zeitpunkt des Tauschs hingen.
        """
        self._backdate_install(self.conti, 30)
        self.advance(hours=3)
        pirelli = self.add_part(self.slot, "Pirelli")

        self.ride(100, days_ago=1)  # jetzt erst synchronisiert

        self.assertEqual(self.wear_km(self.conti), 100)
        self.assertEqual(self.wear_km(pirelli), 0)

    def test_deleted_ride_disappears_from_the_part(self):
        """Alt: lag die Zahl danach unter dem Einbau-km-Stand, stand das Teil auf "unbekannt"."""
        first = self.ride(100)
        self.ride(10)
        self.assertEqual(self.wear_km(self.conti), 110)
        first.delete()
        self.assertEqual(self.wear_km(self.conti), 10)

    def test_ride_moved_to_another_bike_leaves_the_part(self):
        ride = self.ride(100)
        ride.bike = self.make_bike()
        ride.save()
        self.assertEqual(self.wear_km(self.conti), 0)

    def test_ride_on_another_bike_never_counts(self):
        self.ride(100, bike=self.make_bike())
        self.assertEqual(self.wear_km(self.conti), 0)

    def test_ride_on_swap_day_counts_once_on_the_weather_axis_too(self):
        """Alt: das Datums-Fenster schloss den Tausch-Tag bei beiden Reifen ein."""
        self.ride(100)
        pirelli = self.add_part(self.slot, "Pirelli")
        self.ride(50)
        conti_km, _ = WeatherWearService.calculate_weather_wear_km(self.conti)
        pirelli_km, _ = WeatherWearService.calculate_weather_wear_km(pirelli)
        self.assertEqual(conti_km, 100)
        self.assertEqual(pirelli_km, 50)

    def test_bike_recompute_reaches_parts_on_the_shelf(self):
        """Alt: nur montierte Teile wurden neu berechnet, das Regal blieb auf dem Altstand."""
        self._backdate_install(self.conti, 30)
        self.advance(hours=3)
        self.add_part(self.slot, "Pirelli")
        self.ride(100, days_ago=1)
        WeatherWearService.recompute_bike(self.bike)
        self.conti.refresh_from_db()
        self.assertEqual(self.conti.weather_wear_km, 100)

    def test_breakdown_of_an_old_ride_knows_who_was_on(self):
        """Alt: ein zurückgeholter Reifen fehlte in der Auswertung früherer Fahrten."""
        old_ride = self.ride(100)
        pirelli = self.add_part(self.slot, "Pirelli")
        self.mount(self.conti)
        self.ride(10)

        names = [
            part["id"]
            for category in ride_wear_breakdown(old_ride)["categories"]
            for part in category["components"]
        ]
        self.assertIn(self.conti.id, names)
        self.assertNotIn(pirelli.id, names)


class WheelSwitchTests(WheelScenario):
    """
    Der Wechsel Carbon ↔ Alu: Vorder- und Hinterrad gemeinsam, und die Bremsbeläge
    wechseln mit, die Bremse selbst bleibt.
    """

    def setUp(self):
        super().setUp()
        self.alu_front = self.assembly(
            self.front_group, "Alu", self.tire_front, "Alu VR"
        )
        self.alu_rear = self.assembly(self.rear_group, "Alu", self.tire_rear, "Alu HR")
        self.brake = self.assembly(self.brake_group, "", self.pad_rear, "Die Blauen")
        self.blue_pad = self.part_in(self.brake, self.pad_rear)
        self.pad_slot = self.slot_of(self.brake, self.pad_rear)

        self.carbon_front = self.assembly(
            self.front_group, "Carbon", self.tire_front, "Carbon VR"
        )
        self.carbon_rear = self.assembly(
            self.rear_group, "Carbon", self.tire_rear, "Carbon HR"
        )
        self.yellow_pad = self.add_part(self.pad_slot, "Yellow King", mount=False)

        self._link(self.alu_front, assemblies=[self.alu_rear])
        self._link(
            self.carbon_front,
            assemblies=[self.carbon_rear],
        )
        self._link(
            self.carbon_rear, assemblies=[self.carbon_front], parts=[self.yellow_pad]
        )

    def _link(self, assembly, assemblies=(), parts=()):
        return self.client.put(
            f"{P}/assemblies/{assembly.id}/links/",
            {
                "assembly_ids": [a.id for a in assemblies],
                "component_ids": [c.id for c in parts],
            },
            format="json",
        )

    def _refresh(self):
        for item in (
            self.alu_front,
            self.alu_rear,
            self.carbon_front,
            self.carbon_rear,
            self.brake,
            self.blue_pad,
            self.yellow_pad,
        ):
            item.refresh_from_db()

    def test_new_second_set_starts_parked(self):
        self.assertTrue(self.carbon_front.is_parked)
        self.assertTrue(self.alu_front.is_active)

    def test_switching_one_wheel_brings_the_whole_set_and_the_pads(self):
        self.post(f"{P}/assemblies/{self.carbon_front.id}/activate/")
        self._refresh()

        self.assertTrue(self.carbon_front.is_active)
        self.assertTrue(self.carbon_rear.is_active)
        self.assertTrue(self.alu_front.is_parked)
        self.assertTrue(self.alu_rear.is_parked)
        self.assertTrue(self.yellow_pad.is_mounted)
        self.assertTrue(self.blue_pad.is_spare, "Der alte Belag liegt jetzt im Regal.")

    def test_only_the_pad_changes_not_the_brake(self):
        rotor = self.post(
            f"{P}/assemblies/{self.brake.id}/items/",
            {"template_id": self.rotor_rear.id, "brand": "Scheibe"},
        )
        self.post(f"{P}/assemblies/{self.carbon_rear.id}/activate/")
        self._refresh()

        self.assertTrue(self.brake.is_active, "Die Bremse bleibt am Rad.")
        self.assertEqual(
            BikeAssembly.objects.filter(bike=self.bike, group=self.brake_group).count(),
            1,
        )
        self.assertTrue(Component.objects.get(pk=rotor.data["id"]).is_mounted)

    def test_km_follow_the_wheels_and_the_pads(self):
        self.ride(100)
        self.post(f"{P}/assemblies/{self.carbon_front.id}/activate/")
        self.ride(30)
        self.post(f"{P}/assemblies/{self.alu_front.id}/activate/")
        self.ride(7)

        alu_tire = self.part_in(self.alu_front, self.tire_front)
        carbon_tire = self.part_in(self.carbon_front, self.tire_front)
        alu_rear_tire = self.part_in(self.alu_rear, self.tire_rear)
        self.assertEqual(self.wear_km(alu_tire), 107)
        self.assertEqual(self.wear_km(alu_rear_tire), 107)
        self.assertEqual(self.wear_km(carbon_tire), 30)
        self.assertEqual(self.wear_km(self.blue_pad), 107)
        self.assertEqual(self.wear_km(self.yellow_pad), 30)

        self.alu_front.refresh_from_db()
        self.assertEqual(self.alu_front.compute_km(), 107)
        self.assertEqual(self.carbon_rear.compute_km(), 30)

    def test_switching_back_finds_the_old_pad_without_any_setup(self):
        """Nur Carbon ist gekoppelt; der Alu-Belag merkt sich beim Abnehmen sein Laufrad."""
        self.assertFalse(self.alu_rear.linked_components.exists())

        self.post(f"{P}/assemblies/{self.carbon_rear.id}/activate/")
        self.alu_rear.refresh_from_db()
        self.assertEqual(list(self.alu_rear.linked_components.all()), [self.blue_pad])

        self.post(f"{P}/assemblies/{self.alu_rear.id}/activate/")
        self._refresh()
        self.assertTrue(self.blue_pad.is_mounted)
        self.assertTrue(self.yellow_pad.is_spare)

    def test_the_pad_is_remembered_by_the_wheel_it_belongs_to(self):
        """Vorder- und Hinterrad wechseln gemeinsam, der Belag gehört nur zum Hinterrad."""
        self.post(f"{P}/assemblies/{self.carbon_front.id}/activate/")
        self.alu_front.refresh_from_db()
        self.assertFalse(self.alu_front.linked_components.exists())

    def test_preview_promises_what_happens(self):
        preview = self.client.get(f"{P}/assemblies/{self.carbon_front.id}/activate/")
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(
            sorted(a["id"] for a in preview.data["activate"]),
            sorted([self.carbon_front.id, self.carbon_rear.id]),
        )
        self.assertEqual(
            sorted(a["id"] for a in preview.data["park"]),
            sorted([self.alu_front.id, self.alu_rear.id]),
        )
        self.assertEqual(preview.data["mount"][0]["component_id"], self.yellow_pad.id)
        self.assertEqual(preview.data["mount"][0]["replaces"], "Die Blauen")

        # Die Vorschau ändert nichts.
        self.carbon_front.refresh_from_db()
        self.assertTrue(self.carbon_front.is_parked)

        done = self.post(f"{P}/assemblies/{self.carbon_front.id}/activate/")
        self.assertEqual(done.data["plan"]["mount"], preview.data["mount"])
        self.assertTrue(done.data["is_active"])

    def test_preview_flags_a_pad_that_stays_on_without_partner(self):
        self._link(self.alu_rear, assemblies=[self.alu_front], parts=[self.blue_pad])
        self._link(self.carbon_rear, assemblies=[self.carbon_front], parts=[])

        preview = self.client.get(f"{P}/assemblies/{self.carbon_rear.id}/activate/")
        self.assertEqual(preview.data["mount"], [])
        self.assertEqual(preview.data["unpaired"][0]["component_id"], self.blue_pad.id)
        self.assertEqual(preview.data["unpaired"][0]["from_assembly"], "Alu")

    def test_uncoupled_wheel_switches_alone(self):
        self._link(self.carbon_front, assemblies=[])
        self.post(f"{P}/assemblies/{self.carbon_front.id}/activate/")
        self._refresh()
        self.assertTrue(self.carbon_front.is_active)
        self.assertTrue(self.alu_rear.is_active, "Das Hinterrad bleibt, wo es ist.")
        self.assertTrue(self.blue_pad.is_mounted)

    def test_parked_wheel_does_not_collect_while_on_the_other_set(self):
        carbon_tire = self.part_in(self.carbon_front, self.tire_front)
        self.ride(100)
        self.assertEqual(self.wear_km(carbon_tire), 0)

    def test_tire_swapped_on_a_parked_wheel_starts_at_zero(self):
        """Alt: das Teil zeigte "unbekannt" statt 0, bis das Laufrad wieder drauf war."""
        slot = self.slot_of(self.carbon_front, self.tire_front)
        self.ride(100)
        new_tire = self.add_part(slot, "Neu auf Carbon")
        self.assertEqual(self.wear_km(new_tire), 0)
        self.assertEqual(compute_wear(new_tire)["warn_status_km"], "ok")

        self.ride(50)
        self.assertEqual(self.wear_km(new_tire), 0)

        self.post(f"{P}/assemblies/{self.carbon_front.id}/activate/")
        self.ride(20)
        self.assertEqual(self.wear_km(new_tire), 20)

    def test_parked_wheels_stay_out_of_the_bike_status(self):
        """Alt: die überfällige Dichtmilch eines Laufrads im Keller färbte das Rad rot."""
        MaintenanceInterval.objects.create(
            bike=self.bike,
            assembly=self.carbon_front,
            label="Dichtmilch",
            interval_days=120,
            last_done_at=self.days_ago(400),
        )
        res = self.client.get(f"{P}/bikes/{self.bike.id}/")
        self.assertNotEqual(res.data["warn_status"], "critical")

    def test_interval_on_a_parked_wheel_collects_no_km(self):
        """Alt: das Intervall rechnete gegen den Gesamtstand des Rads."""
        interval = MaintenanceInterval.objects.create(
            bike=self.bike,
            assembly=self.carbon_front,
            label="Felgenservice",
            interval_km=100,
            last_done_at=self.days_ago(1),
        )
        self.ride(5000)
        self.assertEqual(interval.km_since_done(), 0)
        self.assertEqual(interval.status(), "ok")

    def test_backdated_switch(self):
        self.ride(100, days_ago=5)
        self.ride(50, days_ago=1)

        for part in (
            self.part_in(self.alu_front, self.tire_front),
            self.part_in(self.alu_rear, self.tire_rear),
            self.blue_pad,
        ):
            self.client.patch(
                f"{P}/components/{part.id}/",
                {"installed_at": str(self.days_ago(10))},
                format="json",
            )
        self.post(
            f"{P}/assemblies/{self.carbon_front.id}/activate/",
            {"on": str(self.days_ago(2))},
        )
        self.assertEqual(
            self.wear_km(self.part_in(self.alu_front, self.tire_front)), 100
        )
        self.assertEqual(
            self.wear_km(self.part_in(self.carbon_front, self.tire_front)), 50
        )
        self.assertEqual(self.wear_km(self.blue_pad), 100)
        self.assertEqual(self.wear_km(self.yellow_pad), 50)

    def test_switch_before_the_wheel_went_on_is_refused(self):
        res = self.post(
            f"{P}/assemblies/{self.carbon_front.id}/activate/",
            {"on": str(self.days_ago(30))},
            expect=400,
        )
        self.assertEqual(res.data["code"], "date_before_mount")
        self._refresh()
        self.assertTrue(self.alu_front.is_active, "Es hat sich nichts geändert.")

    def test_retired_pad_is_not_brought_back(self):
        self.post(f"{P}/components/{self.yellow_pad.id}/retire/")
        self.post(f"{P}/assemblies/{self.carbon_rear.id}/activate/")
        self._refresh()
        self.assertTrue(self.yellow_pad.is_retired)
        self.assertTrue(self.blue_pad.is_mounted)


class WheelRetireTests(WheelScenario):
    """Ausmustern: ein Laufrad, der ganze Satz oder nur einzelne Teile."""

    def setUp(self):
        super().setUp()
        self.alu = self.assembly(self.front_group, "Alu", self.tire_front, "Alu VR")
        self.alu_rear = self.assembly(self.rear_group, "Alu", self.tire_rear, "Alu HR")
        self.carbon = self.assembly(
            self.front_group, "Carbon", self.tire_front, "Carbon VR"
        )
        self.tire = self.part_in(self.alu, self.tire_front)
        self.client.put(
            f"{P}/assemblies/{self.alu.id}/links/",
            {"assembly_ids": [self.alu_rear.id]},
            format="json",
        )

    def test_retiring_a_parked_wheel_adds_no_km(self):
        """Alt der gröbste Fehler: aus 100 km wurden beim Ausmustern 700."""
        self.ride(100)
        self.post(f"{P}/assemblies/{self.carbon.id}/activate/")
        self.ride(500)
        self.assertEqual(self.wear_km(self.tire), 100)

        self.post(f"{P}/assemblies/{self.alu.id}/retire/")
        self.assertEqual(self.wear_km(self.tire), 100)
        self.alu.refresh_from_db()
        self.assertEqual(self.alu.compute_km(), 100)

    def test_retiring_twice_changes_nothing(self):
        """Alt: jeder weitere Aufruf erfand eine zusätzliche Nutzungsperiode."""
        self.ride(100)
        self.post(f"{P}/assemblies/{self.alu.id}/retire/")
        periods = self.alu.periods.count()
        self.post(f"{P}/assemblies/{self.alu.id}/retire/")
        self.assertEqual(self.alu.periods.count(), periods)

    def test_retiring_a_wheel_retires_the_mounted_parts_but_not_the_spares(self):
        slot = self.slot_of(self.alu, self.tire_front)
        spare = self.add_part(slot, "Ersatz", mount=False)

        self.post(f"{P}/assemblies/{self.alu.id}/retire/")

        self.tire.refresh_from_db()
        spare.refresh_from_db()
        self.assertTrue(self.tire.is_retired)
        self.assertTrue(
            spare.is_spare, "Der gute Reifen im Regal bleibt ein Ersatzteil."
        )

        res = self.client.get(f"{P}/bikes/{self.bike.id}/assemblies/")
        listed = {s["id"]: s for s in res.data["spare_components"]}
        self.assertTrue(listed[spare.id]["orphaned"])

    def test_retiring_a_single_part_leaves_the_wheel_alone(self):
        self.post(f"{P}/components/{self.tire.id}/retire/")
        self.alu.refresh_from_db()
        self.assertTrue(self.alu.is_active)
        self.assertIsNone(self.slot_of(self.alu, self.tire_front).mounted_component)

    def test_retiring_the_whole_set(self):
        self.post(f"{P}/assemblies/{self.alu.id}/retire/", {"with_linked": True})
        self.alu.refresh_from_db()
        self.alu_rear.refresh_from_db()
        self.assertTrue(self.alu.is_retired)
        self.assertTrue(self.alu_rear.is_retired)

    def test_retiring_one_wheel_keeps_the_other(self):
        self.post(f"{P}/assemblies/{self.alu.id}/retire/")
        self.alu_rear.refresh_from_db()
        self.assertTrue(self.alu_rear.is_active)

    def test_interval_of_a_retired_wheel_stays_out_of_the_bike_status(self):
        """Alt: das Rad blieb für immer rot, wegen einer Dichtmilch im Müll."""
        MaintenanceInterval.objects.create(
            bike=self.bike,
            assembly=self.alu,
            label="Dichtmilch",
            interval_days=120,
            last_done_at=self.days_ago(400),
        )
        self.post(f"{P}/assemblies/{self.alu.id}/retire/")
        res = self.client.get(f"{P}/bikes/{self.bike.id}/")
        self.assertNotEqual(res.data["warn_status"], "critical")

    def test_retired_wheel_is_listed_and_can_be_restored(self):
        self.ride(100)
        self.post(f"{P}/assemblies/{self.alu.id}/retire/")
        self.ride(50)

        res = self.client.get(f"{P}/bikes/{self.bike.id}/assemblies/")
        self.assertIn(self.alu.id, [a["id"] for a in res.data["retired_assemblies"]])

        self.post(f"{P}/assemblies/{self.alu.id}/restore/")
        self.alu.refresh_from_db()
        self.tire.refresh_from_db()
        self.assertTrue(self.alu.is_active, "Es war nichts anderes aufgezogen.")
        self.assertTrue(self.tire.is_mounted, "Die Teile kommen mit zurück.")
        self.assertIsNone(self.alu.retired_at)

        self.ride(30)
        self.assertEqual(self.wear_km(self.tire), 130, "Die Zeit im Müll zählt nicht.")

    def test_a_set_retired_together_comes_back_together(self):
        self.post(f"{P}/assemblies/{self.alu.id}/retire/", {"with_linked": True})
        self.post(f"{P}/assemblies/{self.alu.id}/restore/")
        self.alu.refresh_from_db()
        self.alu_rear.refresh_from_db()
        self.assertTrue(self.alu.is_active)
        self.assertTrue(self.alu_rear.is_active)
        self.assertTrue(self.part_in(self.alu_rear, self.tire_rear).is_mounted)

    def test_restoring_leaves_a_partner_retired_on_another_day(self):
        self.post(
            f"{P}/assemblies/{self.alu_rear.id}/retire/",
            {"on": str(self.days_ago(0))},
        )
        BikeAssembly.objects.filter(pk=self.alu_rear.pk).update(
            retired_at=self.days_ago(30)
        )
        self.post(f"{P}/assemblies/{self.alu.id}/retire/")
        self.post(f"{P}/assemblies/{self.alu.id}/restore/")
        self.alu_rear.refresh_from_db()
        self.assertTrue(self.alu_rear.is_retired)

    def test_restore_comes_back_parked_when_the_slot_is_taken(self):
        self.post(f"{P}/assemblies/{self.carbon.id}/activate/")
        self.post(f"{P}/assemblies/{self.alu.id}/retire/")
        self.post(f"{P}/assemblies/{self.alu.id}/restore/")
        self.alu.refresh_from_db()
        self.carbon.refresh_from_db()
        self.assertTrue(self.alu.is_parked)
        self.assertTrue(self.carbon.is_active)

    def test_retired_wheel_cannot_be_switched_to(self):
        self.post(f"{P}/assemblies/{self.carbon.id}/retire/")
        res = self.post(f"{P}/assemblies/{self.carbon.id}/activate/", expect=400)
        self.assertEqual(res.data["code"], "retired")

    def test_part_on_a_retired_wheel_cannot_be_mounted_until_it_moved(self):
        slot = self.slot_of(self.alu, self.tire_front)
        spare = self.add_part(slot, "Ersatz", mount=False)
        self.post(f"{P}/assemblies/{self.alu.id}/retire/")

        res = self.post(f"{P}/components/{spare.id}/mount/", expect=400)
        self.assertEqual(res.data["code"], "assembly_retired")

        target = self.slot_of(self.carbon, self.tire_front)
        self.post(f"{P}/components/{spare.id}/move/", {"slot_id": target.id})
        spare.refresh_from_db()
        self.assertEqual(spare.slot_id, target.id)
        self.assertTrue(spare.is_mounted)


class MoveBetweenWheelsTests(WheelScenario):
    """Ein Reifen zieht von einem Laufrad aufs andere und nimmt seine km mit."""

    def setUp(self):
        super().setUp()
        self.alu = self.assembly(self.front_group, "Alu", self.tire_front, "Conti")
        self.carbon = self.assembly(
            self.front_group, "Carbon", self.tire_front, "Pirelli"
        )
        self.conti = self.part_in(self.alu, self.tire_front)
        self.pirelli = self.part_in(self.carbon, self.tire_front)

    def test_tire_keeps_its_km_when_it_moves(self):
        self.ride(100)
        target = self.slot_of(self.carbon, self.tire_front)
        self.post(f"{P}/components/{self.conti.id}/move/", {"slot_id": target.id})

        self.conti.refresh_from_db()
        self.pirelli.refresh_from_db()
        self.assertEqual(self.conti.slot_id, target.id)
        self.assertTrue(self.conti.is_mounted)
        self.assertTrue(self.pirelli.is_spare)
        self.assertEqual(self.wear_km(self.conti), 100)

        # Carbon ist geparkt: kein km, bis es am Rad ist.
        self.ride(50)
        self.assertEqual(self.wear_km(self.conti), 100)

        self.post(f"{P}/assemblies/{self.carbon.id}/activate/")
        self.ride(10)
        self.assertEqual(self.wear_km(self.conti), 110)

    def test_cannot_move_to_a_different_kind_of_slot(self):
        rear = self.assembly(self.rear_group, "Alu", self.tire_rear, "HR")
        target = self.slot_of(rear, self.tire_rear)
        res = self.post(
            f"{P}/components/{self.conti.id}/move/", {"slot_id": target.id}, expect=400
        )
        self.assertEqual(res.data["code"], "slot_mismatch")

    def test_cannot_move_to_a_foreign_bike(self):
        other_bike = self.make_bike()
        slot = ComponentSlot.objects.create(bike=other_bike, template=self.tire_front)
        self.post(
            f"{P}/components/{self.conti.id}/move/", {"slot_id": slot.id}, expect=404
        )

    def test_moved_part_is_still_in_the_old_rides(self):
        ride = self.ride(100)
        target = self.slot_of(self.carbon, self.tire_front)
        self.post(f"{P}/components/{self.conti.id}/move/", {"slot_id": target.id})
        ids = [
            part["id"]
            for category in ride_wear_breakdown(ride)["categories"]
            for part in category["components"]
        ]
        self.assertIn(self.conti.id, ids)


class LinkValidationTests(WheelScenario):
    def setUp(self):
        super().setUp()
        self.alu = self.assembly(self.front_group, "Alu", self.tire_front, "Conti")
        self.alu_rear = self.assembly(self.rear_group, "Alu", self.tire_rear, "HR")
        self.carbon = self.assembly(
            self.front_group, "Carbon", self.tire_front, "Pirelli"
        )
        self.brake = self.assembly(self.brake_group, "", self.pad_rear, "Belag")
        self.pad = self.part_in(self.brake, self.pad_rear)
        self.url = f"{P}/assemblies/{self.alu.id}/links/"

    def _put(self, **body):
        return self.client.put(self.url, body, format="json")

    def test_links_are_replaced_and_reported(self):
        res = self._put(assembly_ids=[self.alu_rear.id], component_ids=[self.pad.id])
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["linked_assembly_ids"], [self.alu_rear.id])
        self.assertEqual(res.data["linked_component_ids"], [self.pad.id])

        res = self._put(assembly_ids=[], component_ids=[])
        self.assertEqual(res.data["linked_component_ids"], [])

    def test_link_between_wheels_is_mutual(self):
        self._put(assembly_ids=[self.alu_rear.id])
        self.alu_rear.refresh_from_db()
        self.assertEqual(list(self.alu_rear.linked_assemblies.all()), [self.alu])

    def test_two_wheels_of_the_same_side_cannot_be_linked(self):
        res = self._put(assembly_ids=[self.carbon.id])
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "link_invalid")

    def test_chain_of_links_cannot_join_the_same_side_either(self):
        self.client.put(
            f"{P}/assemblies/{self.carbon.id}/links/",
            {"assembly_ids": [self.alu_rear.id]},
            format="json",
        )
        res = self._put(assembly_ids=[self.alu_rear.id])
        self.assertEqual(res.status_code, 400)
        self.assertEqual(list(self.alu.linked_assemblies.all()), [])

    def test_a_tire_on_another_wheel_cannot_be_linked(self):
        tire = self.part_in(self.alu_rear, self.tire_rear)
        res = self._put(component_ids=[tire.id])
        self.assertEqual(res.status_code, 400)

    def test_two_parts_for_one_slot_cannot_be_linked(self):
        slot = self.slot_of(self.brake, self.pad_rear)
        second = self.add_part(slot, "Zweiter Belag", mount=False)
        res = self._put(component_ids=[self.pad.id, second.id])
        self.assertEqual(res.status_code, 400)

    def test_foreign_parts_cannot_be_linked(self):
        other = self.make_profile()
        bike = self.make_bike(other)
        slot = ComponentSlot.objects.create(bike=bike, template=self.pad_rear)
        foreign = Component.objects.create(slot=slot, brand="Fremd")
        res = self._put(component_ids=[foreign.id])
        self.assertEqual(res.status_code, 400)


class AreaHasOneInstanceTests(WheelScenario):
    """Bremse, Antrieb & Co. gibt es je Rad einmal: zwei Beläge sind zwei Teile."""

    def setUp(self):
        super().setUp()
        self.brake = self.assembly(self.brake_group, "", self.pad_rear, "Belag")

    def test_second_brake_is_refused_and_points_to_the_first(self):
        res = self.post(
            f"{P}/bikes/{self.bike.id}/assemblies/",
            {"group_id": self.brake_group.id},
            expect=400,
        )
        self.assertEqual(res.data["code"], "area_exists")
        self.assertEqual(res.data["assembly_id"], self.brake.id)

    def test_area_in_use_is_not_offered_again_but_a_wheel_is(self):
        self.assembly(self.front_group, "Alu", self.tire_front, "VR")
        res = self.client.get(f"{P}/bikes/{self.bike.id}/assemblies/")
        offered = {g["id"] for g in res.data["available_groups"]}
        self.assertNotIn(self.brake_group.id, offered)
        self.assertIn(self.front_group.id, offered)

    def test_retired_area_can_be_replaced(self):
        self.post(f"{P}/assemblies/{self.brake.id}/retire/")
        res = self.post(
            f"{P}/bikes/{self.bike.id}/assemblies/",
            {"group_id": self.brake_group.id},
        )
        self.assertTrue(res.data["is_active"])

    def test_restoring_an_area_next_to_a_new_one_is_refused(self):
        self.post(f"{P}/assemblies/{self.brake.id}/retire/")
        self.post(
            f"{P}/bikes/{self.bike.id}/assemblies/", {"group_id": self.brake_group.id}
        )
        res = self.post(f"{P}/assemblies/{self.brake.id}/restore/", expect=400)
        self.assertEqual(res.data["code"], "area_exists")


class CrudTests(WheelScenario):
    """Alles lässt sich anlegen, ändern und löschen, und niemand kommt an Fremdes."""

    def test_several_manual_bikes(self):
        """Alt: das zweite von Hand angelegte Rad (systemweit!) gab einen 500."""
        first = self.post(f"{P}/bikes/", {"name": "Eins", "bike_type": "road"})
        second = self.post(f"{P}/bikes/", {"name": "Zwei", "bike_type": "gravel"})
        self.assertIsNone(first.data["strava_bike_id"])
        self.assertNotEqual(first.data["id"], second.data["id"])

    def test_bike_update_and_delete(self):
        res = self.client.patch(
            f"{P}/bikes/{self.bike.id}/",
            {"name": "Neu", "bike_type": "gravel", "retired": True},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["name"], "Neu")
        self.assertTrue(res.data["retired"])

        res = self.client.delete(f"{P}/bikes/{self.bike.id}/")
        self.assertEqual(res.status_code, 204)
        self.assertFalse(Bike.objects.filter(pk=self.bike.pk).exists())

    def test_rename_survives_the_next_strava_sync(self):
        """Alt: jeder Sync setzte den Namen zurück."""
        bike = Bike.objects.create(
            athlete=self.profile, strava_bike_id="b77", name="Strava-Name"
        )
        self.client.patch(f"{P}/bikes/{bike.id}/", {"name": "Mein Rad"}, format="json")

        athlete = {
            "bikes": [
                {"id": "b77", "name": "Strava-Name"},
                {"id": "b78", "name": "Neu von Strava"},
            ]
        }
        with patch("app_dashboard.api.services.strava_get") as strava_get:
            strava_get.return_value.json.return_value = athlete
            StravaSyncService.sync_bikes(self.profile)

        bike.refresh_from_db()
        self.assertEqual(bike.name, "Mein Rad")
        self.assertTrue(
            Bike.objects.filter(strava_bike_id="b78", name="Neu von Strava").exists()
        )

    def test_assembly_rename(self):
        wheel = self.assembly(self.front_group, "Alu", self.tire_front, "VR")
        res = self.client.patch(
            f"{P}/assemblies/{wheel.id}/", {"name": "Winter"}, format="json"
        )
        self.assertEqual(res.data["display_name"], "Winter")

    def test_assembly_delete_needs_confirmation(self):
        wheel = self.assembly(self.front_group, "Alu", self.tire_front, "VR")
        res = self.client.delete(f"{P}/assemblies/{wheel.id}/")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "confirm_required")
        self.assertEqual(res.data["parts"], 1)
        self.assertTrue(BikeAssembly.objects.filter(pk=wheel.pk).exists())

        res = self.client.delete(f"{P}/assemblies/{wheel.id}/?confirm=true")
        self.assertEqual(res.status_code, 204)
        self.assertFalse(BikeAssembly.objects.filter(pk=wheel.pk).exists())
        self.assertFalse(Component.objects.filter(slot__bike=self.bike).exists())

    def test_empty_assembly_can_be_deleted_without_confirmation(self):
        res = self.post(
            f"{P}/bikes/{self.bike.id}/assemblies/", {"group_id": self.brake_group.id}
        )
        res = self.client.delete(f"{P}/assemblies/{res.data['id']}/")
        self.assertEqual(res.status_code, 204)

    def test_deleting_a_linked_part_or_wheel_cleans_up(self):
        alu = self.assembly(self.front_group, "Alu", self.tire_front, "VR")
        rear = self.assembly(self.rear_group, "Alu", self.tire_rear, "HR")
        brake = self.assembly(self.brake_group, "", self.pad_rear, "Belag")
        pad = self.part_in(brake, self.pad_rear)
        self.client.put(
            f"{P}/assemblies/{alu.id}/links/",
            {"assembly_ids": [rear.id], "component_ids": [pad.id]},
            format="json",
        )
        self.assertEqual(
            self.client.delete(f"{P}/components/{pad.id}/").status_code, 204
        )
        self.client.delete(f"{P}/assemblies/{rear.id}/?confirm=true")
        alu.refresh_from_db()
        self.assertFalse(alu.linked_components.exists())
        self.assertFalse(alu.linked_assemblies.exists())

    def test_slot_rename_and_delete(self):
        wheel = self.assembly(self.front_group, "Alu", self.tire_front, "VR")
        slot = self.slot_of(wheel, self.tire_front)
        res = self.client.patch(
            f"{P}/slots/{slot.id}/", {"custom_name": "Vorne breit"}, format="json"
        )
        self.assertEqual(res.data["display_name"], "Vorne breit")

        self.assertEqual(self.client.delete(f"{P}/slots/{slot.id}/").status_code, 400)
        res = self.client.delete(f"{P}/slots/{slot.id}/?confirm=true")
        self.assertEqual(res.status_code, 204)
        self.assertFalse(Component.objects.filter(slot__bike=self.bike).exists())

    def test_slot_template_is_fixed_after_creation(self):
        wheel = self.assembly(self.front_group, "Alu", self.tire_front, "VR")
        slot = self.slot_of(wheel, self.tire_front)
        self.client.patch(
            f"{P}/slots/{slot.id}/", {"template": self.tire_rear.id}, format="json"
        )
        slot.refresh_from_db()
        self.assertEqual(slot.template_id, self.tire_front.id)

    def test_component_edit_and_delete(self):
        wheel = self.assembly(self.front_group, "Alu", self.tire_front, "VR")
        tire = self.part_in(wheel, self.tire_front)
        res = self.client.patch(
            f"{P}/components/{tire.id}/",
            {
                "brand": "Conti",
                "model_name": "GP5000",
                "custom_warn_km": 3000,
                "notes": "x",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["label"], "Conti GP5000")
        self.assertEqual(res.data["effective_warn_km"], 3000)

        self.assertEqual(
            self.client.delete(f"{P}/components/{tire.id}/").status_code, 204
        )

    def test_retired_assembly_takes_no_new_items(self):
        wheel = self.assembly(self.front_group, "Alu", self.tire_front, "VR")
        self.post(f"{P}/assemblies/{wheel.id}/retire/")
        res = self.post(
            f"{P}/assemblies/{wheel.id}/items/",
            {"template_id": ComponentTemplate.objects.get(name="Felge vorne").id},
            expect=400,
        )
        self.assertEqual(res.data["code"], "assembly_retired")

    def test_interval_crud_and_undo(self):
        created = self.post(
            f"{P}/bikes/{self.bike.id}/intervals/",
            {"label": "Kette ölen", "interval_km": 300},
        )
        interval_id = created.data["id"]

        res = self.client.patch(
            f"{P}/intervals/{interval_id}/",
            {"label": "Kette wachsen", "interval_km": 500},
            format="json",
        )
        self.assertEqual(res.data["label"], "Kette wachsen")

        self.ride(100)
        before = self.client.get(f"{P}/intervals/{interval_id}/").data
        self.assertEqual(before["km_since"], 100)

        done = self.post(f"{P}/intervals/{interval_id}/log/")
        self.assertEqual(done.data["km_since"], 0)

        undone = self.client.delete(f"{P}/intervals/{interval_id}/log/")
        self.assertEqual(undone.status_code, 200)
        self.assertEqual(undone.data["km_since"], 100)
        self.assertIsNone(undone.data["last_log"])

        self.assertEqual(
            self.client.delete(f"{P}/intervals/{interval_id}/log/").status_code, 400
        )
        self.assertEqual(
            self.client.delete(f"{P}/intervals/{interval_id}/").status_code, 204
        )

    def test_check_can_be_taken_back(self):
        wheel = self.assembly(self.front_group, "Alu", self.tire_front, "VR")
        tire = self.part_in(wheel, self.tire_front)
        self.post(f"{P}/components/{tire.id}/check/", {"condition_pct": 80})
        res = self.client.delete(f"{P}/components/{tire.id}/check/")
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.data["last_check"])
        self.assertEqual(
            self.client.delete(f"{P}/components/{tire.id}/check/").status_code, 400
        )

    def test_other_athletes_things_are_out_of_reach(self):
        wheel = self.assembly(self.front_group, "Alu", self.tire_front, "VR")
        tire = self.part_in(wheel, self.tire_front)
        slot = self.slot_of(wheel, self.tire_front)
        interval = MaintenanceInterval.objects.create(bike=self.bike, label="X")

        self.login(self.make_profile())
        for method, url in (
            ("get", f"{P}/bikes/{self.bike.id}/"),
            ("delete", f"{P}/bikes/{self.bike.id}/"),
            ("get", f"{P}/bikes/{self.bike.id}/assemblies/"),
            ("get", f"{P}/assemblies/{wheel.id}/"),
            ("get", f"{P}/assemblies/{wheel.id}/activate/"),
            ("post", f"{P}/assemblies/{wheel.id}/activate/"),
            ("post", f"{P}/assemblies/{wheel.id}/retire/"),
            ("post", f"{P}/assemblies/{wheel.id}/restore/"),
            ("put", f"{P}/assemblies/{wheel.id}/links/"),
            ("delete", f"{P}/assemblies/{wheel.id}/?confirm=true"),
            ("get", f"{P}/slots/{slot.id}/"),
            ("post", f"{P}/slots/{slot.id}/components/"),
            ("delete", f"{P}/slots/{slot.id}/?confirm=true"),
            ("get", f"{P}/components/{tire.id}/"),
            ("post", f"{P}/components/{tire.id}/mount/"),
            ("post", f"{P}/components/{tire.id}/unmount/"),
            ("post", f"{P}/components/{tire.id}/retire/"),
            ("post", f"{P}/components/{tire.id}/restore/"),
            ("post", f"{P}/components/{tire.id}/move/"),
            ("delete", f"{P}/components/{tire.id}/"),
            ("get", f"{P}/intervals/{interval.id}/"),
            ("post", f"{P}/intervals/{interval.id}/log/"),
            ("delete", f"{P}/intervals/{interval.id}/log/"),
        ):
            res = getattr(self.client, method)(url, {"slot_id": slot.id}, format="json")
            self.assertEqual(res.status_code, 404, f"{method} {url}")
        tire.refresh_from_db()
        self.assertTrue(tire.is_mounted)

    def test_actions_need_a_login(self):
        wheel = self.assembly(self.front_group, "Alu", self.tire_front, "VR")
        tire = self.part_in(wheel, self.tire_front)
        self.client.logout()
        for url in (
            f"{P}/components/{tire.id}/mount/",
            f"{P}/components/{tire.id}/retire/",
            f"{P}/assemblies/{wheel.id}/activate/",
            f"{P}/assemblies/{wheel.id}/restore/",
        ):
            self.assertIn(self.client.post(url).status_code, (401, 403), url)


class CustomTemplateTests(WheelScenario):
    """Alt: eigene Vorlagen standen ohne Besitzer in einem Topf — für alle änderbar."""

    def test_own_template_is_private(self):
        res = self.post(
            f"{P}/templates/",
            {
                "name": "Aero-Aufsatz",
                "category": "cockpit",
                "applicable_bike_types": [],
                "warn_km": 5000,
                "group": ComponentGroup.objects.get(name="Cockpit").id,
            },
        )
        template_id = res.data["id"]
        self.assertFalse(res.data["is_system"])

        mine = self.client.get(f"{P}/templates/")
        self.assertIn(template_id, [t["id"] for t in mine.data])
        groups = self.client.get(f"{P}/groups/")
        cockpit = next(g for g in groups.data if g["name"] == "Cockpit")
        self.assertIn(template_id, [t["id"] for t in cockpit["parts"]])

        self.login(self.make_profile())
        theirs = self.client.get(f"{P}/templates/")
        self.assertNotIn(template_id, [t["id"] for t in theirs.data])
        groups = self.client.get(f"{P}/groups/")
        cockpit = next(g for g in groups.data if g["name"] == "Cockpit")
        self.assertNotIn(template_id, [t["id"] for t in cockpit["parts"]])
        self.assertEqual(
            self.client.get(f"{P}/templates/{template_id}/").status_code, 404
        )
        self.assertEqual(
            self.client.patch(
                f"{P}/templates/{template_id}/", {"name": "gehackt"}, format="json"
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.delete(f"{P}/templates/{template_id}/").status_code, 404
        )

    def test_catalog_templates_are_read_only(self):
        res = self.client.patch(
            f"{P}/templates/{self.tire_front.id}/", {"name": "x"}, format="json"
        )
        self.assertEqual(res.status_code, 404)

    def test_template_in_use_is_not_deleted(self):
        group = ComponentGroup.objects.get(name="Cockpit")
        template = ComponentTemplate.objects.create(
            name="Eigenes Teil",
            category="cockpit",
            owner=self.profile,
            is_system=False,
            group=group,
        )
        cockpit = self.post(
            f"{P}/bikes/{self.bike.id}/assemblies/",
            {
                "group_id": group.id,
                "parts": [{"template_id": template.id, "include": True}],
            },
        )
        self.assertEqual(cockpit.status_code, 201)
        res = self.client.delete(f"{P}/templates/{template.id}/")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "template_in_use")

    def test_someone_elses_template_is_no_valid_catalog_entry(self):
        group = ComponentGroup.objects.get(name="Cockpit")
        foreign = ComponentTemplate.objects.create(
            name="Fremd",
            category="cockpit",
            owner=self.make_profile(),
            is_system=False,
            group=group,
        )
        self.post(
            f"{P}/bikes/{self.bike.id}/assemblies/",
            {
                "group_id": group.id,
                "parts": [{"template_id": foreign.id, "include": True}],
            },
            expect=400,
        )
        assembly = self.post(
            f"{P}/bikes/{self.bike.id}/assemblies/", {"group_id": group.id}
        )
        self.post(
            f"{P}/assemblies/{assembly.data['id']}/items/",
            {"template_id": foreign.id},
            expect=400,
        )


class IntervalBaselineTests(WheelScenario):
    def setUp(self):
        super().setUp()
        self.interval = MaintenanceInterval.objects.create(
            bike=self.bike,
            label="Kette",
            interval_km=1000,
            last_done_at=self.days_ago(100),
        )

    def test_backdated_done_counts_the_km_since_that_day(self):
        """Alt: `km_since` stand sofort auf 0, weil der heutige Stand als Basis galt."""
        self.ride(800, days_ago=60)
        self.ride(800, days_ago=10)
        res = self.post(
            f"{P}/intervals/{self.interval.id}/log/",
            {"done_at": str(self.days_ago(30))},
        )
        self.assertEqual(res.data["km_since"], 800)

    def test_done_today_does_not_count_todays_ride(self):
        self.ride(100)
        res = self.post(f"{P}/intervals/{self.interval.id}/log/")
        self.assertEqual(res.data["km_since"], 0)
        self.ride(10)
        self.assertEqual(
            self.client.get(f"{P}/intervals/{self.interval.id}/").data["km_since"], 10
        )

    def test_older_entry_does_not_move_the_baseline_back(self):
        self.post(f"{P}/intervals/{self.interval.id}/log/")
        self.ride(100)
        res = self.post(
            f"{P}/intervals/{self.interval.id}/log/",
            {"done_at": str(self.days_ago(50))},
        )
        self.assertEqual(res.data["km_since"], 100)

    def test_future_done_date_is_refused(self):
        res = self.post(
            f"{P}/intervals/{self.interval.id}/log/",
            {"done_at": str(self.today() + timedelta(days=3))},
            expect=400,
        )
        self.assertEqual(res.data["code"], "future_date")

    def test_correcting_the_date_moves_the_baseline(self):
        self.ride(100, days_ago=20)
        res = self.client.patch(
            f"{P}/intervals/{self.interval.id}/",
            {"last_done_at": str(self.days_ago(10))},
            format="json",
        )
        self.assertEqual(res.data["km_since"], 0)
        res = self.client.patch(
            f"{P}/intervals/{self.interval.id}/",
            {"last_done_at": str(self.days_ago(30))},
            format="json",
        )
        self.assertEqual(res.data["km_since"], 100)


class BulkInstalledAtOnParkedWheelsTests(WheelScenario):
    def test_date_for_the_whole_bike_leaves_a_parked_wheel_alone(self):
        """Alt: das schob den Einbau des Kellersatzes hinter seine Fahrten, seine km waren weg."""
        carbon = self.assembly(self.front_group, "Carbon", self.tire_front, "Carbon")
        tire = self.part_in(carbon, self.tire_front)
        self.ride(100)
        alu = self.assembly(self.front_group, "Alu", self.tire_front, "Alu")
        self.post(f"{P}/assemblies/{alu.id}/activate/")
        self.ride(100)
        self.assertEqual(self.wear_km(tire), 100)

        self.post(
            f"{P}/bikes/{self.bike.id}/installed-at/",
            {"installed_at": str(self.today())},
        )
        self.assertEqual(self.wear_km(tire), 100)


class NotificationDedupeTests(WheelScenario):
    def test_warning_mail_comes_again_after_a_release(self):
        """
        Alt: der Docstring versprach es, aber nach einer Freigabe stand der Dedupe weiter auf
        dem alten Status. Wurde das Teil wieder kritisch, kam keine Mail mehr.
        """
        wheel = self.assembly(self.front_group, "Alu", self.tire_front, "VR")
        tire = self.part_in(wheel, self.tire_front)
        self.ride(6000)
        Component.objects.filter(pk=tire.pk).update(
            last_warn_notified_status="critical"
        )
        self.assertEqual(get_new_component_warnings(self.profile), [])

        self.post(f"{P}/components/{tire.id}/check/")
        self.ride(6000)

        warned = [w["component"].pk for w in get_new_component_warnings(self.profile)]
        self.assertIn(tire.id, warned)

    def test_parts_on_a_parked_wheel_send_no_mail(self):
        carbon = self.assembly(self.front_group, "Carbon", self.tire_front, "Carbon")
        self.ride(6000)
        alu = self.assembly(self.front_group, "Alu", self.tire_front, "Alu")
        self.post(f"{P}/assemblies/{alu.id}/activate/")
        warned = [w["component"].pk for w in get_new_component_warnings(self.profile)]
        self.assertNotIn(self.part_in(carbon, self.tire_front).pk, warned)


class StatusFieldTests(WheelScenario):
    def test_database_rejects_a_second_mounted_part_in_a_slot(self):
        wheel = self.assembly(self.front_group, "Alu", self.tire_front, "VR")
        slot = self.slot_of(wheel, self.tire_front)
        spare = self.add_part(slot, "Ersatz", mount=False)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Component.objects.filter(pk=spare.pk).update(status=ComponentStatus.MOUNTED)

    def test_status_transitions_keep_the_period_invariant(self):
        wheel = self.assembly(self.front_group, "Alu", self.tire_front, "VR")
        tire = self.part_in(wheel, self.tire_front)

        def is_open():
            return tire.periods.filter(ended_at__isnull=True).exists()

        self.assertTrue(is_open())
        self.post(f"{P}/components/{tire.id}/unmount/")
        self.assertFalse(is_open())
        self.mount(tire)
        self.assertTrue(is_open())

        other = self.assembly(self.front_group, "Carbon", self.tire_front, "C")
        self.post(f"{P}/assemblies/{other.id}/activate/")
        self.assertFalse(is_open())
        tire.refresh_from_db()
        self.assertTrue(tire.is_mounted, "Sitzt weiter auf dem Laufrad im Keller.")

    def test_assembly_period_follows_its_status(self):
        wheel = self.assembly(self.front_group, "Alu", self.tire_front, "VR")
        self.assertTrue(wheel.periods.filter(ended_at__isnull=True).exists())
        wheel.status = AssemblyStatus.PARKED
        wheel.save()
        self.assertFalse(wheel.periods.filter(ended_at__isnull=True).exists())
