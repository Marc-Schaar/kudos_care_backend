import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags

from app_auth.models import StravaProfile

logger = logging.getLogger("my_app_debug")


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
        text_body = strip_tags(html_body)
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
