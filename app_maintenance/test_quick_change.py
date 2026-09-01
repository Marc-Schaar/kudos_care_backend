from datetime import date, timedelta

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from app_auth.models import StravaProfile
from app_maintenance.models import (
    Bike,
    BikeType,
    Component,
    ComponentCategory,
    ComponentGroup,
    ComponentSlot,
    ComponentTemplate,
)


def _make_profile(user, strava_athlete_id=13579):
    return StravaProfile.objects.create(
        user=user,
        strava_athlete_id=strava_athlete_id,
        access_token="token",
        refresh_token="refresh",
        expires_at=0,
    )


class SlotQuickChangeViewTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="quickchange", password="pw"
        )
        self.profile = _make_profile(self.user)
        self.client.force_login(self.user)
        session = self.client.session
        session["strava_athlete_id"] = self.profile.strava_athlete_id
        session.save()

        self.bike = Bike.objects.create(
            athlete=self.profile,
            strava_bike_id="qc1",
            name="Laufradrad",
            bike_type=BikeType.GRAVEL,
        )

        self.group_front = ComponentGroup.objects.create(name="Laufrad vorne")
        self.tire_template = ComponentTemplate.objects.create(
            name="Reifen vorne",
            category=ComponentCategory.WHEELS,
            is_system=False,
            group=self.group_front,
        )
        self.rim_template = ComponentTemplate.objects.create(
            name="Felge vorne",
            category=ComponentCategory.WHEELS,
            is_system=False,
            group=self.group_front,
        )
        self.hub_template = ComponentTemplate.objects.create(
            name="Nabenlager vorne",
            category=ComponentCategory.WHEELS,
            is_system=False,
            group=self.group_front,
        )
        # Ungruppiertes Template zur Kontrolle
        self.chain_template = ComponentTemplate.objects.create(
            name="Kette", category=ComponentCategory.DRIVETRAIN, is_system=False
        )

        self.tire_slot = ComponentSlot.objects.create(
            bike=self.bike, template=self.tire_template
        )
        self.rim_slot = ComponentSlot.objects.create(
            bike=self.bike, template=self.rim_template
        )
        self.hub_slot = ComponentSlot.objects.create(
            bike=self.bike, template=self.hub_template
        )
        self.chain_slot = ComponentSlot.objects.create(
            bike=self.bike, template=self.chain_template
        )

        self.old_tire = Component.objects.create(
            slot=self.tire_slot,
            brand="Schwalbe",
            installed_at=date.today() - timedelta(days=200),
            is_mounted=True,
        )
        self.old_rim = Component.objects.create(
            slot=self.rim_slot,
            brand="DT Swiss",
            installed_at=date.today() - timedelta(days=200),
            is_mounted=True,
        )
        # Nabenlager bleibt unmontiert/leer

    def test_get_returns_sibling_slots_of_same_group(self):
        response = self.client.get(
            f"/api/maintenance/slots/{self.tire_slot.id}/quick-change/"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["group"]["name"], "Laufrad vorne")
        returned_slot_ids = {item["slot_id"] for item in response.data["items"]}
        self.assertEqual(
            returned_slot_ids, {self.tire_slot.id, self.rim_slot.id, self.hub_slot.id}
        )
        self.assertNotIn(self.chain_slot.id, returned_slot_ids)

        tire_item = next(
            i for i in response.data["items"] if i["slot_id"] == self.tire_slot.id
        )
        self.assertEqual(tire_item["mounted_component"]["brand"], "Schwalbe")
        hub_item = next(
            i for i in response.data["items"] if i["slot_id"] == self.hub_slot.id
        )
        self.assertIsNone(hub_item["mounted_component"])

    def test_get_on_slot_without_group_returns_404(self):
        response = self.client.get(
            f"/api/maintenance/slots/{self.chain_slot.id}/quick-change/"
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data["code"], "no_group")

    def test_post_swaps_only_included_items_with_shared_date_and_own_brand(self):
        response = self.client.post(
            f"/api/maintenance/slots/{self.tire_slot.id}/quick-change/",
            {
                "installed_at": "2026-08-01",
                "items": [
                    {
                        "slot_id": self.tire_slot.id,
                        "include": True,
                        "brand": "Continental",
                        "model_name": "GP5000",
                    },
                    {"slot_id": self.rim_slot.id, "include": False},
                    {"slot_id": self.hub_slot.id, "include": True, "brand": "Hope"},
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Reifen: alte Komponente ausgebaut, neue montiert
        self.old_tire.refresh_from_db()
        self.assertFalse(self.old_tire.is_mounted)
        self.assertIsNotNone(self.old_tire.retired_at)
        new_tire = self.tire_slot.components.get(is_mounted=True)
        self.assertEqual(new_tire.brand, "Continental")
        self.assertEqual(str(new_tire.installed_at), "2026-08-01")

        # Felge: include=False -> unangetastet
        self.old_rim.refresh_from_db()
        self.assertTrue(self.old_rim.is_mounted)

        # Nabenlager: vorher leer, jetzt neu montiert
        new_hub = self.hub_slot.components.get(is_mounted=True)
        self.assertEqual(new_hub.brand, "Hope")
        self.assertEqual(str(new_hub.installed_at), "2026-08-01")

        # Response enthält nur die tatsächlich geänderten Slots
        returned_slot_ids = {slot["id"] for slot in response.data}
        self.assertEqual(returned_slot_ids, {self.tire_slot.id, self.hub_slot.id})

    def test_post_rejects_slot_id_outside_group(self):
        response = self.client.post(
            f"/api/maintenance/slots/{self.tire_slot.id}/quick-change/",
            {
                "items": [
                    {"slot_id": self.chain_slot.id, "include": True, "brand": "X"},
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # Keine Seiteneffekte
        self.chain_slot.refresh_from_db()
        self.assertIsNone(self.chain_slot.mounted_component)

    def test_post_defaults_installed_at_to_today(self):
        response = self.client.post(
            f"/api/maintenance/slots/{self.tire_slot.id}/quick-change/",
            {
                "items": [
                    {
                        "slot_id": self.tire_slot.id,
                        "include": True,
                        "brand": "Continental",
                    },
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        new_tire = self.tire_slot.components.get(is_mounted=True)
        self.assertEqual(new_tire.installed_at, date.today())

    def test_requires_auth(self):
        self.client.logout()
        response = self.client.get(
            f"/api/maintenance/slots/{self.tire_slot.id}/quick-change/"
        )
        self.assertIn(
            response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )

    def test_only_one_mounted_component_per_slot_invariant_holds_after_quick_change(
        self,
    ):
        self.client.post(
            f"/api/maintenance/slots/{self.tire_slot.id}/quick-change/",
            {
                "items": [
                    {
                        "slot_id": self.tire_slot.id,
                        "include": True,
                        "brand": "Continental",
                    },
                ],
            },
            format="json",
        )
        self.assertEqual(self.tire_slot.components.filter(is_mounted=True).count(), 1)
