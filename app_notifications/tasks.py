import logging

from celery import shared_task

from .services import send_templated_email

logger = logging.getLogger("my_app_debug")


@shared_task
def send_welcome_email_task(profile_id: int):
    """
    Verschickt die Willkommens-E-Mail. Wird sowohl automatisch bei Erstanmeldung
    ausgeloest (siehe app_auth/api/views.py::StravaAuthCallbackView) als auch manuell
    ueber die Admin-Action "Willkommens-E-Mail senden" auf StravaProfile.
    """
    from django.utils import timezone

    from app_auth.models import StravaProfile

    try:
        profile = StravaProfile.objects.select_related("user").get(pk=profile_id)
    except StravaProfile.DoesNotExist:
        logger.warning(
            "send_welcome_email_task: Profil %s existiert nicht mehr.", profile_id
        )
        return f"Profil {profile_id} nicht gefunden."

    sent = send_templated_email(
        profile,
        subject="Willkommen bei Kudos Care",
        template_name="emails/welcome.html",
        context={},
    )
    if sent:
        profile.welcome_email_sent_at = timezone.now()
        profile.save(update_fields=["welcome_email_sent_at"])
    return f"Willkommens-Mail fuer Profil {profile_id}: {'gesendet' if sent else 'nicht gesendet'}."


def _send_component_warning_digest(profile, bike=None) -> bool:
    """
    Interner Helper (kein eigener Celery-Task): sammelt neue Warn-/Critical-Komponenten
    des Profils (optional auf ein Bike eingeschraenkt), verschickt bei Treffern eine
    Sammel-E-Mail und aktualisiert bei Erfolg Component.last_warn_notified_status. Wird
    von check_component_warnings_for_bike (event-getriggert) und check_component_warnings
    (taeglicher Voll-Scan) gemeinsam genutzt, damit die Logik nicht doppelt existiert.
    """
    from app_maintenance.api.services import get_new_component_warnings

    warnings = get_new_component_warnings(profile, bike=bike)
    if not warnings:
        return False

    count = len(warnings)
    if count == 1:
        subject = "Kudos Care: 1 Komponente benötigt Aufmerksamkeit"
    else:
        subject = f"Kudos Care: {count} Komponenten benötigen Aufmerksamkeit"

    sent = send_templated_email(
        profile,
        subject=subject,
        template_name="emails/component_warnings.html",
        context={"warnings": warnings, "immediate": bike is not None},
    )
    if sent:
        for warning in warnings:
            component = warning["component"]
            component.last_warn_notified_status = warning["wear"]["warn_status_overall"]
            component.save(update_fields=["last_warn_notified_status"])
    return sent


@shared_task
def check_component_warnings_for_bike(bike_id: int):
    """
    Event-getriggerter Task: wird direkt nach jeder erfolgreichen
    recompute_weather_wear_for_bike-Ausfuehrung angestossen (siehe
    app_maintenance/api/tasks.py), meldet also unmittelbar nach einer Fahrt, wenn dieses
    eine Bike dadurch eine neu warn/critical gewordene Komponente hat.
    """
    from app_maintenance.models import Bike

    try:
        bike = Bike.objects.select_related("athlete__user").get(pk=bike_id)
    except Bike.DoesNotExist:
        logger.warning(
            "check_component_warnings_for_bike: Bike %s existiert nicht mehr.", bike_id
        )
        return f"Bike {bike_id} nicht gefunden."

    sent = _send_component_warning_digest(bike.athlete, bike=bike)
    return f"Bike {bike_id}: Warn-Mail {'versendet' if sent else 'nicht noetig'}."


@shared_task
def check_component_warnings():
    """
    Taeglicher Beat-Task, Sicherheitsnetz fuer rein kalender-getriebene Faelle (Komponente
    wird allein durch Zeitablauf ueberfaellig, ganz ohne neue Fahrt — das kann der
    event-getriggerte Check nicht abdecken). Iteriert alle Profile mit aktivierten
    Benachrichtigungen und hinterlegter E-Mail-Adresse.
    """
    from app_auth.models import StravaProfile

    profiles = (
        StravaProfile.objects.filter(
            email_notifications_enabled=True, user__isnull=False
        )
        .exclude(user__email="")
        .select_related("user")
    )

    sent_count = 0
    for profile in profiles:
        if _send_component_warning_digest(profile):
            sent_count += 1
    return f"{sent_count} Warn-Digest-Mail(s) versendet."


@shared_task
def check_bike_unsafe_predictions():
    """
    Taeglicher Beat-Task fuer die Vorhersage-Mail: warnt vor Bikes, die laut
    Fahrt-Vorhersage bis zur naechsten voraussichtlichen Fahrt kritisch werden, obwohl sie
    heute noch nicht kritisch sind (siehe app_maintenance.api.services.get_predicted_unsafe_bikes).
    """
    from app_auth.models import StravaProfile
    from app_maintenance.api.services import get_predicted_unsafe_bikes

    profiles = (
        StravaProfile.objects.filter(
            email_notifications_enabled=True, user__isnull=False
        )
        .exclude(user__email="")
        .select_related("user")
    )

    sent_count = 0
    for profile in profiles:
        predictions = get_predicted_unsafe_bikes(profile)
        if not predictions:
            continue

        sent = send_templated_email(
            profile,
            subject="Kudos Care: Vor der nächsten Fahrt prüfen",
            template_name="emails/bike_unsafe_predictions.html",
            context={"predictions": predictions},
        )
        if sent:
            sent_count += 1
            for prediction in predictions:
                bike = prediction["bike"]
                bike.predicted_unsafe_notified_for_date = prediction["predicted_date"]
                bike.save(update_fields=["predicted_unsafe_notified_for_date"])
    return f"{sent_count} Vorhersage-Mail(s) versendet."
