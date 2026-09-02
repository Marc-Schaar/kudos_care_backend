"""
Datenschutz-Zusicherungen rund um die E-Mail-Adresse und das Löschen.

Die Adresse ist der einzige direkt personenbezogene Wert, den die App selbst
erhebt — gebraucht wird sie ausschließlich zum Versenden
(`app_notifications/services.py`). Deshalb wird sie nicht gehasht (an einen Hash
lässt sich nichts senden), sondern an den Stellen entfernt, an denen sie ohne
Zweck sichtbar wäre: im Admin und in den Logs.
"""

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APITestCase

from app_auth.models import StravaProfile
from app_dashboard.models import Ride
from app_maintenance.models import (
    Bike,
    BikeType,
    Component,
    ComponentCategory,
    ComponentSlot,
    ComponentTemplate,
)


class AdminHidesEmailTests(TestCase):
    """Im Admin darf die Adresse weder stehen noch auffindbar sein."""

    def test_user_admin_shows_no_email(self):
        user_admin = admin.site._registry[User]
        self.assertNotIn("email", user_admin.list_display)
        flat = [field for _, opts in user_admin.fieldsets for field in opts["fields"]]
        self.assertNotIn("email", flat)

    def test_user_admin_is_not_searchable_by_email(self):
        """
        Eine Suche verrät die Adresse auch ohne Anzeige: wer sie eintippt und
        einen Treffer bekommt, weiß Bescheid.
        """
        user_admin = admin.site._registry[User]
        self.assertNotIn("email", user_admin.search_fields)

    def test_profile_admin_is_not_searchable_by_email(self):
        profile_admin = admin.site._registry[StravaProfile]
        self.assertNotIn("user__email", profile_admin.search_fields)


class EmailStaysOutOfLogsTests(TestCase):
    """Logdateien leben länger und werden breiter gelesen als die Datenbank."""

    def test_failed_send_logs_the_athlete_id_not_the_address(self):
        from app_notifications.services import send_templated_email

        user = get_user_model().objects.create_user(
            username="logtest", email="geheim@example.com"
        )
        profile = StravaProfile.objects.create(
            user=user, strava_athlete_id=515151, expires_at=0
        )

        with self.assertLogs("my_app_debug", level="ERROR") as captured:
            # Ein nicht existierendes Template laesst den Versand scheitern und
            # damit den Logger laufen.
            ok = send_templated_email(
                profile, "Betreff", "emails/gibt_es_nicht.html", {}
            )

        self.assertFalse(ok)
        joined = "\n".join(captured.output)
        self.assertNotIn("geheim@example.com", joined)
        self.assertIn("515151", joined)


class AccountDeletionTests(APITestCase):
    """Art. 17 DSGVO — das Konto und alles daran Hängende muss löschbar sein."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="del", email="weg@example.com"
        )
        self.profile = StravaProfile.objects.create(
            user=self.user, strava_athlete_id=606060, expires_at=0
        )
        self.client.force_login(self.user)
        session = self.client.session
        session["strava_athlete_id"] = 606060
        session.save()

        self.bike = Bike.objects.create(
            athlete=self.profile,
            strava_bike_id="del1",
            name="Rad",
            bike_type=BikeType.ROAD,
        )
        Ride.objects.create(
            strava_id=999001,
            name="Fahrt",
            distance=10_000,
            athlete=self.profile,
            bike=self.bike,
        )
        template = ComponentTemplate.objects.create(
            name="Kette-Del", category=ComponentCategory.DRIVETRAIN, warn_km=3000
        )
        slot = ComponentSlot.objects.create(bike=self.bike, template=template)
        Component.objects.create(slot=slot, brand="X", is_mounted=True)

    def test_deletion_requires_explicit_confirmation(self):
        res = self.client.delete("/api/strava/me/")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data["code"], "confirmation_required")
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())

    def test_deletion_removes_the_user_and_everything_attached(self):
        res = self.client.delete("/api/strava/me/?confirm=true")
        self.assertEqual(res.status_code, status.HTTP_204_NO_CONTENT)

        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())
        self.assertFalse(StravaProfile.objects.filter(pk=self.profile.pk).exists())
        self.assertFalse(Bike.objects.filter(pk=self.bike.pk).exists())
        self.assertEqual(Ride.objects.count(), 0)
        self.assertEqual(ComponentSlot.objects.count(), 0)
        self.assertEqual(Component.objects.count(), 0)

    def test_session_is_gone_afterwards(self):
        self.client.delete("/api/strava/me/?confirm=true")
        res = self.client.get("/api/strava/me/")
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_other_athletes_are_untouched(self):
        other_user = get_user_model().objects.create_user(username="bleibt")
        other_profile = StravaProfile.objects.create(
            user=other_user, strava_athlete_id=707070, expires_at=0
        )
        other_bike = Bike.objects.create(
            athlete=other_profile,
            strava_bike_id="keep1",
            name="Fremd",
            bike_type=BikeType.ROAD,
        )

        self.client.delete("/api/strava/me/?confirm=true")

        self.assertTrue(User.objects.filter(pk=other_user.pk).exists())
        self.assertTrue(Bike.objects.filter(pk=other_bike.pk).exists())

    def test_requires_authentication(self):
        self.client.logout()
        res = self.client.delete("/api/strava/me/?confirm=true")
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)
