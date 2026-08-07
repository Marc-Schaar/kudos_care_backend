import logging

from celery import shared_task

logger = logging.getLogger("my_app_debug")


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def recompute_weather_wear_for_bike(self, bike_id: int):
    """
    Berechnet weather_wear_km für alle aktuell montierten Komponenten eines
    Bikes neu. Wird als Fire-and-forget-Seiteneffekt nach jedem Ride-Import
    ausgelöst (siehe app_dashboard/api/services.py::sync_activity_to_db).
    """
    from app_maintenance.models import Bike
    from app_maintenance.api.services import WeatherWearService
    from app_notifications.tasks import check_component_warnings_for_bike

    try:
        bike = Bike.objects.get(pk=bike_id)
    except Bike.DoesNotExist:
        logger.warning("recompute_weather_wear_for_bike: Bike %s existiert nicht mehr.", bike_id)
        return f"Bike {bike_id} nicht gefunden."

    try:
        count = WeatherWearService.recompute_bike(bike)
        # Nach der Neuberechnung koennen sich warn_status_overall-Werte geaendert haben
        # (km- und wetter-Achse) — direkt pruefen, ob eine Warn-E-Mail faellig ist, statt
        # bis zum naechsten taeglichen Check zu warten (siehe app_notifications).
        check_component_warnings_for_bike.delay(bike.id)
        return f"{count} Komponenten fuer Bike {bike_id} neu berechnet."
    except Exception as exc:
        logger.exception("Wetter-Verschleiss-Neuberechnung fuer Bike %s fehlgeschlagen.", bike_id)
        raise self.retry(exc=exc)
