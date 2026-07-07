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
    ComponentSlot,
    ComponentTemplate,
)


def _make_profile(user, strava_athlete_id=12345):
    return StravaProfile.objects.create(
        user=user,
        strava_athlete_id=strava_athlete_id,
        access_token="token",
        refresh_token="refresh",
        expires_at=0,
    )


class ComponentCheckTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="athlete", password="pw")
        self.profile = _make_profile(self.user)
        self.client.force_login(self.user)
        session = self.client.session
        session["strava_athlete_id"] = self.profile.strava_athlete_id
        session.save()

        self.bike = Bike.objects.create(
            athlete=self.profile,
            strava_bike_id="b1",
            name="Testrad",
            bike_type=BikeType.ROAD,
        )

    def _make_slot_with_component(self, warn_days=10, supports_condition_estimate=True, custom_warn_days=None):
        template = ComponentTemplate.objects.create(
            name="Bremsbeläge",
            category=ComponentCategory.BRAKES,
            warn_days=warn_days,
            supports_condition_estimate=supports_condition_estimate,
            is_system=False,
        )
        slot = ComponentSlot.objects.create(bike=self.bike, template=template)
        component = Component.objects.create(
            slot=slot,
            brand="Shimano",
            installed_at=date.today() - timedelta(days=100),
            custom_warn_days=custom_warn_days,
            is_mounted=True,
        )
        return template, slot, component

    def test_create_component_with_custom_warn_days(self):
        _, slot, _ = self._make_slot_with_component()

        response = self.client.post(
            f"/api/maintenance/slots/{slot.id}/components/",
            {
                "brand": "Shimano",
                "model_name": "XT",
                "installed_at": date.today().isoformat(),
                "is_mounted": False,
                "custom_warn_days": 30,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["custom_warn_days"], 30)

    def test_component_overdue_without_check_is_critical(self):
        _, _, component = self._make_slot_with_component(warn_days=10)

        response = self.client.get(f"/api/maintenance/components/{component.id}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["warn_status_overall"], "critical")
        self.assertIsNone(response.data["last_check"])

    def test_check_rejects_condition_pct_when_not_supported(self):
        _, _, component = self._make_slot_with_component(supports_condition_estimate=False)

        response = self.client.post(
            f"/api/maintenance/components/{component.id}/check/",
            {"condition_pct": 50},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_check_releases_overdue_component_and_stores_snooze(self):
        _, _, component = self._make_slot_with_component(warn_days=10)

        response = self.client.post(
            f"/api/maintenance/components/{component.id}/check/",
            {"condition_pct": 50, "snooze_days": 5, "note": "sieht noch gut aus"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["warn_status_overall"], "ok")
        self.assertEqual(response.data["last_check"]["condition_pct"], 50)
        self.assertEqual(response.data["last_check"]["snooze_days"], 5)

    def test_check_requires_authentication(self):
        _, _, component = self._make_slot_with_component()
        self.client.logout()

        response = self.client.post(f"/api/maintenance/components/{component.id}/check/", {})

        self.assertIn(
            response.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN)
        )
