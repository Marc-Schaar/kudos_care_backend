"""
Rückdatiertes Einbaudatum — der Fall "Fahrten sind schon da, Bike kommt später".

Der Fehler dahinter: `api/usage.py` schneidet die km eines Teils gegen die
Nutzungsperioden seiner Baugruppe. Wer eine Baugruppe heute anlegt und danach
die Teile auf ihr echtes Einbaudatum korrigiert, verschiebt die Teile — die
Periode blieb aber auf "heute" stehen, das Fenster war leer, und alle Teile
zeigten 0 km. Nachgestellt an echten Produktivdaten (Scott Foil RC 10: Teile
seit 30.03. bei 5 km, Periode ab 02.09. bei 1.208 km, angezeigt 0 km).
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from rest_framework import status

from app_auth.models import StravaProfile
from app_maintenance.api.usage import start_of_day
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
    MaintenanceInterval,
    MaintenanceKind,
)
from app_maintenance.testing import MaintenanceTestCase


class _InstallBase(MaintenanceTestCase):
    """Gemeinsamer Aufbau: ein Bike mit 10 Fahrten à 100 km, täglich."""

    def setUp(self):
        super().setUp()
        # 10 Fahrten à 100 km, täglich; die älteste vor 10 Tagen.
        for i in range(10):
            self.ride(100, days_ago=10 - i)

        self.group = ComponentGroup.objects.create(
            name="Antrieb-T",
            category=ComponentCategory.DRIVETRAIN,
            kind=GroupKind.AREA,
        )
        self.chain = ComponentTemplate.objects.create(
            name="Kette",
            category=ComponentCategory.DRIVETRAIN,
            group=self.group,
            warn_km=3000,
            maintenance_kind=MaintenanceKind.PART,
        )
        self.crank = ComponentTemplate.objects.create(
            name="Kurbel",
            category=ComponentCategory.DRIVETRAIN,
            group=self.group,
            warn_km=30000,
            maintenance_kind=MaintenanceKind.PART,
        )
        self.backdated = self.days_ago(5)  # davor liegen 5, ab da 5 Fahrten

    def _create_assembly_today(self) -> int:
        """Baugruppe mit dem Default-Datum (heute) anlegen — der reale Ablauf."""
        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/",
            {
                "group_id": self.group.id,
                "parts": [
                    {"template_id": self.chain.id, "include": True, "brand": "A"},
                    {"template_id": self.crank.id, "include": True, "brand": "B"},
                ],
                "intervals": [],
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        return res.data["id"]

    def _wear(self, component: Component) -> float:
        return self.wear_km(component)


class BackdatedInstallTests(_InstallBase):
    def test_assembly_create_with_a_backdated_date_is_consistent(self):
        """Wird das Datum gleich beim Anlegen gesetzt, passt alles zusammen."""
        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/",
            {
                "group_id": self.group.id,
                "installed_at": str(self.backdated),
                "parts": [{"template_id": self.chain.id, "include": True}],
                "intervals": [],
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        comp = Component.objects.get(slot__template=self.chain)
        self.assertEqual(comp.installed_at, self.backdated)
        self.assertEqual(self._wear(comp), 500.0)  # Fahrten seit dem Morgen des Einbaus

    def test_editing_a_component_backwards_pulls_the_period_with_it(self):
        """
        Der gemeldete Fehler: Baugruppe heute angelegt, Teil danach auf das echte
        Datum korrigiert — vorher blieben die km auf 0 stehen.
        """
        self._create_assembly_today()
        comp = Component.objects.get(slot__template=self.chain)
        self.assertEqual(self._wear(comp), 0.0)  # heute eingebaut, noch nichts gefahren

        res = self.client.patch(
            f"/api/maintenance/components/{comp.id}/",
            {"installed_at": str(self.backdated)},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        self.assertEqual(self._wear(comp), 500.0)
        period = AssemblyUsagePeriod.objects.get(assembly__bike=self.bike)
        self.assertEqual(period.started_at, start_of_day(self.backdated))

    def test_period_start_is_never_pushed_forward(self):
        """
        Ein später eingebautes Einzelteil verkürzt die Laufzeit der Baugruppe
        nicht — die Periode beschreibt, seit wann der Satz am Rad ist.
        """
        assembly_id = self._create_assembly_today()
        comp = Component.objects.get(slot__template=self.chain)
        self.client.patch(
            f"/api/maintenance/components/{comp.id}/",
            {"installed_at": str(self.backdated)},
            format="json",
        )
        period = BikeAssembly.objects.get(pk=assembly_id).open_period()
        self.assertEqual(period.started_at, start_of_day(self.backdated))

        crank = Component.objects.get(slot__template=self.crank)
        self.client.patch(
            f"/api/maintenance/components/{crank.id}/",
            {"installed_at": str(self.days_ago(2))},
            format="json",
        )
        period.refresh_from_db()
        self.assertEqual(period.started_at, start_of_day(self.backdated))


class BulkInstalledAtTests(_InstallBase):
    """`POST bikes/<id>/installed-at/` — ein Datum für alle Teile auf einmal."""

    def test_sets_every_mounted_component_and_the_period(self):
        self._create_assembly_today()

        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/installed-at/",
            {"installed_at": str(self.backdated)},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["components_updated"], 2)
        self.assertEqual(res.data["assemblies_updated"], 1)

        for template in (self.chain, self.crank):
            comp = Component.objects.get(slot__template=template)
            self.assertEqual(comp.installed_at, self.backdated)
            self.assertEqual(self._wear(comp), 500.0)

        period = AssemblyUsagePeriod.objects.get(assembly__bike=self.bike)
        self.assertEqual(period.started_at, start_of_day(self.backdated))

    def test_can_be_limited_to_one_assembly(self):
        first = self._create_assembly_today()

        other_group = ComponentGroup.objects.create(
            name="Cockpit-T", category=ComponentCategory.COCKPIT, kind=GroupKind.AREA
        )
        saddle = ComponentTemplate.objects.create(
            name="Sattel",
            category=ComponentCategory.COCKPIT,
            group=other_group,
            warn_km=20000,
            maintenance_kind=MaintenanceKind.PART,
        )
        self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/",
            {
                "group_id": other_group.id,
                "parts": [{"template_id": saddle.id, "include": True}],
                "intervals": [],
            },
            format="json",
        )

        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/installed-at/",
            {"installed_at": str(self.backdated), "assembly_id": first},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["components_updated"], 2)

        # Der Sattel der anderen Baugruppe bleibt unberührt.
        untouched = Component.objects.get(slot__template=saddle)
        self.assertEqual(untouched.installed_at, self.today())

    def test_future_dates_are_refused(self):
        self._create_assembly_today()
        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/installed-at/",
            {"installed_at": str(self.today() + timedelta(days=1))},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data["code"], "future_date")

    def test_foreign_bike_is_404(self):
        other_user = get_user_model().objects.create_user(username="other")
        other_profile = StravaProfile.objects.create(
            user=other_user,
            strava_athlete_id=8888,
            access_token="t",
            refresh_token="r",
            expires_at=0,
        )
        foreign = Bike.objects.create(
            athlete=other_profile,
            strava_bike_id="x1",
            name="Fremd",
            bike_type=BikeType.ROAD,
        )
        res = self.client.post(
            f"/api/maintenance/bikes/{foreign.id}/installed-at/",
            {"installed_at": str(self.backdated)},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

    def test_requires_authentication(self):
        self.client.logout()
        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/installed-at/",
            {"installed_at": str(self.backdated)},
            format="json",
        )
        self.assertIn(
            res.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )


class AssemblyAddItemTests(_InstallBase):
    """
    `POST assemblies/<id>/items/` — ein einzelnes Element nachtragen.

    Vorher liess sich eine vergessene Kassette nur ergaenzen, indem man die
    ganze Baugruppe neu anlegte.
    """

    def setUp(self):
        super().setUp()
        self.lube = ComponentTemplate.objects.create(
            name="Kettenoel",
            category=ComponentCategory.DRIVETRAIN,
            group=self.group,
            warn_km=300,
            maintenance_kind=MaintenanceKind.CONSUMABLE,
        )
        res = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assemblies/",
            {
                "group_id": self.group.id,
                "parts": [{"template_id": self.chain.id, "include": True}],
                "intervals": [],
            },
            format="json",
        )
        self.assembly_id = res.data["id"]

    def test_adds_a_part_with_its_own_install_point(self):
        res = self.client.post(
            f"/api/maintenance/assemblies/{self.assembly_id}/items/",
            {
                "template_id": self.crank.id,
                "brand": "Shimano",
                "installed_at": str(self.backdated),
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        comp = Component.objects.get(slot__template=self.crank)
        self.assertEqual(comp.brand, "Shimano")
        self.assertEqual(comp.installed_at, self.backdated)
        self.assertEqual(self._wear(comp), 500.0)

    def test_a_backdated_part_pulls_the_period_with_it(self):
        self.client.post(
            f"/api/maintenance/assemblies/{self.assembly_id}/items/",
            {"template_id": self.crank.id, "installed_at": str(self.backdated)},
            format="json",
        )
        period = AssemblyUsagePeriod.objects.get(assembly_id=self.assembly_id)
        self.assertEqual(period.started_at, start_of_day(self.backdated))

    def test_consumable_becomes_an_interval_not_a_part(self):
        res = self.client.post(
            f"/api/maintenance/assemblies/{self.assembly_id}/items/",
            {"template_id": self.lube.id},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertFalse(ComponentSlot.objects.filter(template=self.lube).exists())
        self.assertTrue(
            MaintenanceInterval.objects.filter(
                assembly_id=self.assembly_id, template=self.lube
            ).exists()
        )

    def test_duplicate_is_refused(self):
        res = self.client.post(
            f"/api/maintenance/assemblies/{self.assembly_id}/items/",
            {"template_id": self.chain.id},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data["code"], "already_present")

    def test_template_from_another_group_is_refused(self):
        other_group = ComponentGroup.objects.create(
            name="Cockpit-X", category=ComponentCategory.COCKPIT, kind=GroupKind.AREA
        )
        foreign = ComponentTemplate.objects.create(
            name="Sattel",
            category=ComponentCategory.COCKPIT,
            group=other_group,
            warn_km=20000,
            maintenance_kind=MaintenanceKind.PART,
        )
        res = self.client.post(
            f"/api/maintenance/assemblies/{self.assembly_id}/items/",
            {"template_id": foreign.id},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data["code"], "template_not_in_group")


class PeriodOverlapTests(_InstallBase):
    """
    Rückdatieren darf nicht in eine Zeit reichen, in der die Baugruppe im Keller lag.

    Sonst zählt das Teil Fahrten mit, die ein anderes Laufrad gefahren ist, und die
    Baugruppe steht über der Gesamtleistung des Rads (an Produktivdaten gemessen: 19.691 km
    bei einem Rad mit 9.868 km).
    """

    def test_backdating_skips_the_time_the_assembly_was_off(self):
        assembly_id = self._create_assembly_today()
        assembly = BikeAssembly.objects.get(pk=assembly_id)
        chain = Component.objects.get(slot__template=self.chain)

        # Vor 10 bis vor 6 Tagen am Rad, dann im Keller, seit heute wieder.
        assembly.periods.create(
            started_at=start_of_day(self.days_ago(10)),
            ended_at=start_of_day(self.days_ago(6)),
        )

        self.client.patch(
            f"/api/maintenance/components/{chain.id}/",
            {"installed_at": str(self.days_ago(9))},
            format="json",
        )

        # Drei Fahrten aus der ersten Zeit am Rad; die vier im Keller zählen nicht.
        self.assertEqual(self._wear(chain), 300.0)
        self.assertLessEqual(
            assembly.compute_km(),
            self.bike.total_distance_km,
            "Eine Baugruppe kann nicht mehr km haben als das Rad.",
        )
