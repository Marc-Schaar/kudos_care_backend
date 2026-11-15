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

from app_notifications.presentation import (
    group_predictions,
    group_warnings,
    prediction_subject,
    warning_headline,
    warning_subject,
)
from app_notifications.services import html_to_plaintext

DEFAULT_OUTPUT_DIR = "email_previews"


def _part(label: str, warn_km=None, warn_days=None, weather_km=None) -> SimpleNamespace:
    return SimpleNamespace(
        label=label,
        effective_warn_km=warn_km,
        effective_warn_days=warn_days,
        weather_wear_km=weather_km,
    )


def _sample_context() -> dict:
    """
    Beispieldaten in derselben Form, die `app_notifications.tasks` übergibt, und durch
    dieselben Helfer aus `presentation.py` geschickt — sonst prüft die Vorschau ein
    anderes Layout als das, das verschickt wird. SimpleNamespace statt Models, weil die
    Templates per Attribut zugreifen.
    """
    gravel = SimpleNamespace(id=1, name="Gravel Bike")
    road = SimpleNamespace(id=2, name="Renner")
    today = datetime.date.today()

    warnings = [
        {
            "bike": gravel,
            "slot": SimpleNamespace(display_name="Bremsbeläge vorne"),
            "component": _part("SwissStop Exotherm2", warn_km=2000, weather_km=1712),
            "wear": {"wear_km": 1650.0, "wear_days": 95, "warn_status_overall": "warn"},
        },
        {
            "bike": gravel,
            "slot": SimpleNamespace(display_name="Kette"),
            "component": _part("Shimano CN-M8100", warn_km=3000, weather_km=3312),
            "wear": {
                "wear_km": 2980.4,
                "wear_days": 180,
                "warn_status_overall": "critical",
            },
        },
        {
            "bike": road,
            "slot": SimpleNamespace(display_name="Tubeless-Dichtmilch hinten"),
            "component": _part("", warn_days=120),
            "wear": {
                "wear_km": 1063.0,
                "wear_days": 143,
                "warn_status_overall": "critical",
            },
        },
    ]

    predicted = today + datetime.timedelta(days=4)
    predictions = [
        {
            "bike": gravel,
            "predicted_date": predicted,
            "predicted_km": 54.6,
            "weather": {
                "condition": "Regen",
                "temp_min": 4.2,
                "temp_max": 9.6,
                "precipitation_mm": 3.4,
                "precipitation_probability": 70,
                "wind_kmh": 24.0,
                "gusts_kmh": 46.0,
                "wind_from": "West",
                "wear": {
                    "driver": "rain",
                    "text": "Bei Nässe verschleißt der Antrieb etwa 45 % schneller."
                    " Die Kette danach reinigen und ölen.",
                },
            },
            "components": [
                {
                    "slot": SimpleNamespace(display_name="Reifen hinten"),
                    "component": _part(
                        "Schwalbe G-One RS", warn_km=4000, warn_days=730
                    ),
                    "wear": {
                        "wear_km": 1200.0,
                        "wear_days": 734,
                        "warn_status_overall": "critical",
                    },
                }
            ],
        }
    ]

    warning_groups = group_warnings(warnings)
    prediction_groups = group_predictions(predictions)
    return {
        "welcome.html": ("Willkommen bei Kudos Care", {}),
        "component_warnings.html": (
            warning_subject(warning_groups),
            {
                "groups": warning_groups,
                "headline": warning_headline(warning_groups),
                "immediate": True,
            },
        ),
        "bike_unsafe_predictions.html": (
            prediction_subject(prediction_groups),
            {"groups": prediction_groups},
        ),
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

        for template_name, (subject, context) in _sample_context().items():
            full_context = {**context, "frontend_url": settings.FRONTEND_URL}
            html = render_to_string(f"emails/{template_name}", full_context)

            target = out_dir / template_name
            target.write_text(html, encoding="utf-8")
            self.stdout.write(f"{target}  Betreff: {subject}")

            if options["text"]:
                # Bewusst als Datei statt auf stdout: die Windows-Konsole kann die
                # verwendeten Sonderzeichen nicht ausgeben und wirft UnicodeEncodeError.
                text_target = target.with_suffix(".txt")
                text_target.write_text(html_to_plaintext(html), encoding="utf-8")
                self.stdout.write(f"{text_target}")

            if options["open"]:
                webbrowser.open(target.resolve().as_uri())

        self.stdout.write(self.style.SUCCESS(f"Previews in {out_dir}/ geschrieben."))
