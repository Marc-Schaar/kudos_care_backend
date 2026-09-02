"""
Die Links in den Mails müssen auf das echte Frontend zeigen.

Anlass: `FRONTEND_URL` war in der Produktions-`.env` nie gesetzt, also griff
stillschweigend der Entwicklungs-Default `http://localhost:3000`. Jede
verschickte Warn- und Willkommens-Mail verlinkte damit auf den Rechner des
Empfängers. Aufgefallen ist das erst an echten Mails — genau die Sorte Fehler,
die ein Default verdeckt.
"""

import re

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings

from app_auth.models import StravaProfile
from app_notifications.services import send_templated_email


class FrontendUrlInEmailsTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(
            username="linktest", email="empfaenger@example.com"
        )
        self.profile = StravaProfile.objects.create(
            user=user, strava_athlete_id=828282, expires_at=0
        )

    @override_settings(FRONTEND_URL="https://kudoscare.example.com")
    def test_links_use_the_configured_frontend_url(self):
        ok = send_templated_email(self.profile, "Willkommen", "emails/welcome.html", {})
        self.assertTrue(ok)
        self.assertEqual(len(mail.outbox), 1)

        html = mail.outbox[0].alternatives[0][0]
        links = re.findall(r'href="([^"]+)"', html)
        self.assertTrue(links, "Die Mail sollte mindestens einen Link enthalten.")
        for link in links:
            if link.startswith("mailto:"):
                continue
            self.assertNotIn(
                "localhost",
                link,
                "Kein Link darf auf localhost zeigen — das war der Produktionsfehler.",
            )
            self.assertTrue(
                link.startswith("https://kudoscare.example.com"),
                f"Unerwartetes Linkziel: {link}",
            )

    @override_settings(FRONTEND_URL="https://anders.example.org")
    def test_no_template_hardcodes_a_host(self):
        """
        Die Basis-URL darf ausschliesslich aus der Einstellung kommen. Steht sie
        irgendwo im Template fest, faellt das erst in Produktion auf — genau so
        ist der localhost-Fehler entstanden.
        """
        send_templated_email(self.profile, "Willkommen", "emails/welcome.html", {})
        html = mail.outbox[0].alternatives[0][0]
        for link in re.findall(r'href="(https?://[^"]+)"', html):
            self.assertTrue(
                link.startswith("https://anders.example.org"),
                f"Link ignoriert FRONTEND_URL: {link}",
            )
