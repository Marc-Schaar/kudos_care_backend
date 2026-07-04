from django.core.management.base import BaseCommand, CommandError

from app_strava_webhook.api.tasks import process_strava_webhook


class Command(BaseCommand):
    help = (
        "Reiht process_strava_webhook erneut für Aktivitäten ein, die zuvor "
        "endgültig fehlgeschlagen sind (z.B. wegen des 401-Token-Bugs). "
        "Jede Aktivität wird als 'activity_id' oder 'activity_id:athlete_id' angegeben."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "activities",
            nargs="+",
            help="Aktivitäts-IDs, optional mit Athlet-ID: 19146213877:12345",
        )
        parser.add_argument(
            "--athlete-id",
            type=int,
            default=None,
            help="Athlet-ID für alle Aktivitäten ohne eigene :athlete_id-Angabe.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Nur anzeigen, was eingereiht würde, ohne Tasks zu starten.",
        )

    def handle(self, *args, **options):
        default_athlete_id = options["athlete_id"]
        dry_run = options["dry_run"]

        jobs = []
        for entry in options["activities"]:
            if ":" in entry:
                activity_id_str, athlete_id_str = entry.split(":", 1)
                athlete_id = int(athlete_id_str)
            else:
                activity_id_str = entry
                if default_athlete_id is None:
                    raise CommandError(
                        f"Keine Athlet-ID für Aktivität {entry} angegeben. "
                        "Nutze 'activity_id:athlete_id' oder --athlete-id."
                    )
                athlete_id = default_athlete_id

            jobs.append((int(activity_id_str), athlete_id))

        for activity_id, athlete_id in jobs:
            if dry_run:
                self.stdout.write(
                    f"[dry-run] würde einreihen: activity_id={activity_id} athlete_id={athlete_id}"
                )
                continue

            process_strava_webhook.delay(
                {
                    "object_id": activity_id,
                    "owner_id": athlete_id,
                    "aspect_type": "create",
                }
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"Eingereiht: activity_id={activity_id} athlete_id={athlete_id}"
                )
            )

        self.stdout.write(f"{len(jobs)} Aktivität(en) verarbeitet.")
