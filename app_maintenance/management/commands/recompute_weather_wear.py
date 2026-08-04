from django.core.management.base import BaseCommand

from app_maintenance.models import Component
from app_maintenance.api.services import WeatherWearService


class Command(BaseCommand):
    help = (
        "Berechnet weather_wear_km fuer alle aktuell montierten Komponenten neu, basierend "
        "auf der kompletten Fahrten-Historie des jeweiligen Bikes seit Einbau. Backfill fuer "
        "Komponenten, die vor Einfuehrung des Wetter-Verschleiss-Features eingebaut wurden."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Nur anzeigen, welche Komponenten aktualisiert wuerden, ohne zu speichern.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        components = Component.objects.filter(is_mounted=True).select_related(
            "slot__bike", "slot__template"
        )

        updated = 0
        skipped = 0

        for component in components:
            if not component.installed_at:
                skipped += 1
                self.stdout.write(
                    f"Komponente {component.id} ({component}): kein installed_at, uebersprungen."
                )
                continue

            old_value = component.weather_wear_km
            try:
                if dry_run:
                    new_value, ride_count = WeatherWearService.calculate_weather_wear_km(component)
                    self.stdout.write(
                        f"[dry-run] Komponente {component.id} ({component}): "
                        f"weather_wear_km {old_value} -> {new_value} ({ride_count} Fahrten)"
                    )
                else:
                    WeatherWearService.recompute_component(component)
                    self.stdout.write(
                        f"Komponente {component.id} ({component}): weather_wear_km {old_value} -> "
                        f"{component.weather_wear_km} ({component.weather_wear_ride_count} Fahrten)"
                    )
                updated += 1
            except Exception as e:
                skipped += 1
                self.stderr.write(
                    self.style.WARNING(
                        f"Komponente {component.id} ({component}): Fehler bei der Berechnung: {e}"
                    )
                )

        self.stdout.write(
            self.style.SUCCESS(f"{updated} Komponenten aktualisiert, {skipped} uebersprungen.")
        )
