"""
Regressionstests gegen N+1-Queries in den Bike-/Baugruppen-Endpoints.

Bewusst **keine** festen Query-Zahlen: die wären bei jeder harmlosen
Serializer-Änderung rot. Geprüft wird die eigentliche Invariante — die
Query-Anzahl darf nicht mit der Anzahl der Slots wachsen. Genau das war
verletzt:

* `Bike.total_distance_km` ist ein Property mit eigener Aggregat-Query und
  wurde in den `get_warn_status`-Schleifen je Slot und je Intervall erneut
  gelesen (gemessen 18 identische `SUM`-Queries für ein einziges Bike);
* `ComponentSlot.mounted_component` filterte mit `.filter(is_mounted=True)`
  auf einem prefetchten Related-Manager und umging damit den Prefetch-Cache;
* `ComponentTemplateSerializer.group_name` griff je Template auf
  `template.group` zu, ohne dass die Kette geprefetcht war;
* Slots, die über eine `BikeAssembly` geladen werden, kennen ihr Bike nicht
  (Django cached bei diesem Prefetch-Pfad nur `slot.assembly`).

Scheitert einer dieser Tests, ist meist eine Prefetch-Kette in `api/views.py`
nicht mehr vollständig oder ein Serializer-Feld greift auf eine nicht
mitgeladene Relation zu.
"""

from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from app_auth.models import StravaProfile
from app_dashboard.models import Ride
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
    MaintenanceInterval,
)


class QueryScalingTests(APITestCase):
    """
    Baut je Messung einen eigenen Athleten mit einer definierten Slot-Anzahl
    und vergleicht die Query-Anzahl desselben Endpoints.
    """

    def _make_athlete(self, suffix: str, slot_count: int) -> Bike:
        user = get_user_model().objects.create_user(
            username=f"qc{suffix}", password="pw"
        )
        profile = StravaProfile.objects.create(
            user=user,
            strava_athlete_id=90000 + int(suffix),
            access_token="t",
            refresh_token="r",
            expires_at=0,
        )
        bike = Bike.objects.create(
            athlete=profile,
            strava_bike_id=f"qc{suffix}",
            name=f"Rad {suffix}",
            bike_type=BikeType.GRAVEL,
        )
        for i in range(5):
            Ride.objects.create(
                strava_id=int(f"9{suffix}{i:03d}"),
                name=f"Fahrt {i}",
                distance=20_000,  # 5 x 20 km -> total_distance_km == 100.0
                start_date=timezone.now() - timedelta(days=30 - i),
                athlete=profile,
                bike=bike,
            )

        group = ComponentGroup.objects.create(
            name=f"Gruppe {suffix}", category=ComponentCategory.DRIVETRAIN
        )
        assembly = BikeAssembly.objects.create(
            bike=bike, group=group, installed_at=date.today() - timedelta(days=30)
        )
        AssemblyUsagePeriod.objects.create(
            assembly=assembly,
            started_at=date.today() - timedelta(days=30),
            started_distance_km=0,
        )
        for i in range(slot_count):
            template = ComponentTemplate.objects.create(
                name=f"Teil {suffix}-{i}",
                category=ComponentCategory.DRIVETRAIN,
                warn_km=3000,
                group=group,
            )
            slot = ComponentSlot.objects.create(
                bike=bike, assembly=assembly, template=template
            )
            Component.objects.create(
                slot=slot,
                brand="Marke",
                installed_at=date.today() - timedelta(days=30),
                distance_at_install=0,
                is_mounted=True,
            )
        for i in range(3):
            MaintenanceInterval.objects.create(
                bike=bike,
                assembly=assembly,
                label=f"Intervall {suffix}-{i}",
                interval_km=500,
                last_done_at=date.today() - timedelta(days=30),
                last_done_distance_km=0,
            )
        return bike

    def _login_as(self, bike: Bike) -> None:
        self.client.force_login(bike.athlete.user)
        session = self.client.session
        session["strava_athlete_id"] = bike.athlete.strava_athlete_id
        session.save()

    def _queries_for(self, bike: Bike, url_template: str) -> int:
        self._login_as(bike)
        url = url_template.format(bike_id=bike.id)
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, url)
        return len(ctx.captured_queries)

    def _assert_flat(self, url_template: str) -> None:
        few = self._queries_for(self._make_athlete("1", slot_count=3), url_template)
        many = self._queries_for(self._make_athlete("2", slot_count=15), url_template)
        self.assertEqual(
            few,
            many,
            f"{url_template}: {few} Queries bei 3 Slots, {many} bei 15 — die "
            "Anzahl darf nicht mit den Slots wachsen (fehlender Prefetch?).",
        )

    def test_bike_list_does_not_scale_with_slots(self):
        self._assert_flat("/api/maintenance/bikes/")

    def test_bike_detail_does_not_scale_with_slots(self):
        self._assert_flat("/api/maintenance/bikes/{bike_id}/")

    def test_assembly_list_does_not_scale_with_slots(self):
        self._assert_flat("/api/maintenance/bikes/{bike_id}/assemblies/")

    def test_bike_list_does_not_load_ride_geometry(self):
        """
        Die Gesamtdistanz kommt als Annotation (`with_total_distance()`), nicht
        über ein `prefetch_related("rides")` — sonst landet je Fahrt der
        LineString-Track und das weather_data-JSON im Speicher, ohne dass ein
        Serializer sie je anfasst.
        """
        bike = self._make_athlete("3", slot_count=5)
        self._login_as(bike)
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get("/api/maintenance/bikes/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        with_track = [q for q in ctx.captured_queries if "track" in q["sql"]]
        self.assertEqual(
            with_track, [], "Ride-Geometrie wird geladen, obwohl niemand sie liest."
        )

    def test_total_distance_km_uses_annotation_when_present(self):
        """`with_total_distance()` macht das Property query-frei."""
        bike = self._make_athlete("4", slot_count=2)
        annotated = Bike.objects.with_total_distance().get(pk=bike.pk)
        with CaptureQueriesContext(connection) as ctx:
            value = annotated.total_distance_km
        self.assertEqual(value, 100.0)
        self.assertEqual(ctx.captured_queries, [])

    def test_total_distance_km_falls_back_without_annotation(self):
        """Ohne Annotation bleibt das bisherige Verhalten: eigene Aggregat-Query."""
        bike = self._make_athlete("5", slot_count=2)
        plain = Bike.objects.get(pk=bike.pk)
        self.assertEqual(plain.total_distance_km, 100.0)

    def test_mounted_component_uses_prefetch_cache(self):
        """
        `mounted_component` darf den Prefetch-Cache nicht umgehen — ein
        `.filter()` auf einem prefetchten Related-Manager setzt je Slot eine
        eigene Query ab.
        """
        bike = self._make_athlete("6", slot_count=4)
        slots = list(
            ComponentSlot.objects.filter(bike=bike).prefetch_related("components")
        )
        with CaptureQueriesContext(connection) as ctx:
            mounted = [slot.mounted_component for slot in slots]
        self.assertEqual(ctx.captured_queries, [])
        self.assertEqual(len(mounted), 4)
        self.assertTrue(all(c is not None and c.is_mounted for c in mounted))
