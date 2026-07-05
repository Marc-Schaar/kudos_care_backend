from django.core.management.base import BaseCommand

from app_dashboard.models import Ride
from app_dashboard.api.services import WeatherService
from app_dashboard.api.utils import get_filtered_weather


class Command(BaseCommand):
    help = (
        "Berechnet weather_data (insbesondere den Gegenwind-Zeitverlauf) fuer bereits "
        "importierte Fahrten neu, auf Basis der gespeicherten GPS-Streams. Noetig, weil "
        "aeltere Fahrten mit einer einzigen Start-Ziel-Gerade statt der tatsaechlichen "
        "Position pro Zeitpunkt berechnet wurden."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Nur anzeigen, welche Fahrten aktualisiert wuerden, ohne zu speichern.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        rides = Ride.objects.filter(
            start_latlng__isnull=False,
            track__isnull=False,
            streams__isnull=False,
        ).select_related("streams")

        updated = 0
        skipped = 0

        for ride in rides:
            if not ride.start_date:
                skipped += 1
                continue

            lat = ride.start_latlng.coords[1]
            lon = ride.start_latlng.coords[0]
            start_date = ride.start_date.strftime("%Y-%m-%d")

            weather_info = WeatherService.get_historical_weather(lat, lon, start_date)
            if not weather_info:
                skipped += 1
                self.stdout.write(f"Ride {ride.id}: keine Wetterdaten erhalten, uebersprungen.")
                continue

            stream_data = {
                "latlng": {"data": ride.streams.latlngs},
                "time": {"data": ride.streams.time_series},
            }

            avg_headwind = WeatherService.analyze_wind(stream_data, weather_info, ride)
            new_weather_data = {
                **get_filtered_weather(ride, weather_info, stream_data),
                "avg_headwind": avg_headwind,
            }

            old_avg = (ride.weather_data or {}).get("avg_headwind")

            if dry_run:
                self.stdout.write(
                    f"[dry-run] Ride {ride.id} ({ride.name}): "
                    f"avg_headwind {old_avg} -> {avg_headwind}"
                )
            else:
                ride.weather_data = new_weather_data
                ride.save(update_fields=["weather_data"])
                self.stdout.write(
                    f"Ride {ride.id} ({ride.name}): avg_headwind {old_avg} -> {avg_headwind}"
                )

            updated += 1

        self.stdout.write(
            self.style.SUCCESS(f"{updated} Fahrten aktualisiert, {skipped} uebersprungen.")
        )
