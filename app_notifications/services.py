import logging
import re
from html import unescape

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags

from app_auth.models import StravaProfile

logger = logging.getLogger("my_app_debug")

# strip_tags() entfernt nur die Tags, nicht deren Inhalt — ohne diese Vorabreinigung
# landet das komplette Stylesheet im Plaintext-Teil der Mail.
_HEAD_BLOCK_RE = re.compile(r"<(style|script|head)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_BLANK_LINES_RE = re.compile(r"\n\s*\n\s*\n+")


def html_to_plaintext(html: str) -> str:
    """
    Lesbarer Plaintext-Fallback: Stylesheet raus, Tags raus, HTML-Entities aufgeloest,
    Leerzeilen normalisiert.

    `strip_tags()` allein reicht nicht: es entfernt nur die Tags, nicht den Inhalt von
    <style>, und laesst Entities wie `&middot;` woertlich stehen.
    """
    without_head = _HEAD_BLOCK_RE.sub("", html)
    text = unescape(strip_tags(without_head))
    lines = [line.strip() for line in text.splitlines()]
    return _BLANK_LINES_RE.sub("\n\n", "\n".join(lines)).strip()


def send_templated_email(
    profile: StravaProfile, subject: str, template_name: str, context: dict
) -> bool:
    """
    Rendert `template_name` (erwartet ein {% extends "emails/base_email.html" %}-Template)
    und verschickt es an profile.user.email. Faengt jeden Fehler ab und gibt False zurueck
    statt zu werfen — analog zu app_maintenance.api.ai_providers.BaseAIProvider.generate_text
    ("darf niemals ungefangen in den Request-/Task-Zyklus werfen"). Der Rueckgabewert steuert,
    ob die aufrufende Task Dedupe-Felder aktualisiert (nur bei erfolgreichem Versand — so holt
    der naechste Lauf einen fehlgeschlagenen Versand automatisch nach, statt ihn
    stillschweigend zu verlieren).
    """
    if not profile.email_notifications_enabled:
        return False

    user = profile.user
    if user is None or not user.email:
        logger.info(
            "send_templated_email: Profil %s hat keine E-Mail-Adresse, ueberspringe.",
            profile.pk,
        )
        return False

    full_context = {**context, "frontend_url": settings.FRONTEND_URL}
    try:
        html_body = render_to_string(template_name, full_context)
        text_body = html_to_plaintext(html_body)
        message = EmailMultiAlternatives(
            subject=subject, body=text_body, to=[user.email]
        )
        message.attach_alternative(html_body, "text/html")
        message.send()
        return True
    except Exception:
        logger.exception(
            "E-Mail-Versand an %s (Template %s) fehlgeschlagen.",
            user.email,
            template_name,
        )
        return False
