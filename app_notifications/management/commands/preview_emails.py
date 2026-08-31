"""
Rendert alle E-Mail-Templates mit Beispieldaten in HTML-Dateien.

Damit laesst sich das Mail-Design im Browser pruefen, ohne echte Mails zu verschicken
oder erst passende Warn-Zustaende in der DB herzustellen. Beruehrt weder DB noch
SMTP — reines Template-Rendering.
"""

import datetime
import webbrowser
from pathlib import Path
from types import SimpleNamespace

from django.conf import settings
from django.core.management.base import BaseCommand
from django.template.loader import render_to_string
from app_notifications.services import html_to_plaintext

DEFAULT_OUTPUT_DIR = "email_previews"


def _sample_context() -> dict:
    """
    Beispieldaten in derselben Form, die `app_notifications.tasks` uebergibt —
    verschachtelte Objekte, daher SimpleNamespace statt Dicts (die Templates greifen
    per Attribut zu, z.B. `w.slot.display_name`).
    """
    bike = SimpleNamespace(id=1, name="Gravel Bike")
    today = datetime.date.today()

    warnings = [
        {
            "bike": bike,
            "slot": SimpleNamespace(display_name="Kette"),
            "wear": {
                "wear_km": 4210.5,
                "wear_days": 180,
                "warn_status_overall": "critical",
            },
        },
        {
            "bike": bike,
            "slot": SimpleNamespace(display_name="Bremsbeläge vorne"),
            "wear": {
                "wear_km": 1650.0,
                "wear_days": 95,
                "warn_status_overall": "warn",
            },
        },
    ]

    predictions = [
        {
            "bike": bike,
            "predicted_date": today + datetime.timedelta(days=4),
            "components": [
                {
                    "slot": SimpleNamespace(display_name="Kette"),
                    "wear": {"wear_days": 184, "warn_status_overall": "critical"},
                }
            ],
        }
    ]

    return {
        "welcome.html": {},
        "component_warnings.html": {"warnings": warnings, "immediate": True},
        "bike_unsafe_predictions.html": {"predictions": predictions},
    }


class Command(BaseCommand):
    help = (
        "Rendert alle E-Mail-Templates mit Beispieldaten als HTML-Dateien, um das "
        "Design ohne echten Versand pruefen zu koennen."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--out",
            default=DEFAULT_OUTPUT_DIR,
            help=f"Zielverzeichnis (Default: {DEFAULT_OUTPUT_DIR}/).",
        )
        parser.add_argument(
            "--open",
            action="store_true",
            help="Erzeugte Dateien anschliessend im Standard-Browser oeffnen.",
        )
        parser.add_argument(
            "--text",
            action="store_true",
            help=(
                "Zusaetzlich den Plaintext-Fallback als .txt schreiben (das, was Clients "
                "ohne HTML-Darstellung zu sehen bekommen)."
            ),
        )

    def handle(self, *args, **options):
        out_dir = Path(options["out"])
        out_dir.mkdir(parents=True, exist_ok=True)

        for template_name, context in _sample_context().items():
            full_context = {**context, "frontend_url": settings.FRONTEND_URL}
            html = render_to_string(f"emails/{template_name}", full_context)

            target = out_dir / template_name
            target.write_text(html, encoding="utf-8")
            self.stdout.write(f"{target}")

            if options["text"]:
                # Bewusst als Datei statt auf stdout: die Windows-Konsole kann die
                # verwendeten Sonderzeichen nicht ausgeben und wirft UnicodeEncodeError.
                text_target = target.with_suffix(".txt")
                text_target.write_text(html_to_plaintext(html), encoding="utf-8")
                self.stdout.write(f"{text_target}")

            if options["open"]:
                webbrowser.open(target.resolve().as_uri())

        self.stdout.write(self.style.SUCCESS(f"Previews in {out_dir}/ geschrieben."))
