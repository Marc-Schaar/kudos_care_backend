"""
Tests fuer `manage.py ai_diagnose`.

Kernaussage: der Befehl darf niemals einen echten Key-Wert ausgeben, egal was der
Provider zurueckmeldet — und er muss die Ausfall-Arten unterscheiden koennen
(fehlender Key / ungueltiger Key / Rate-Limit / falscher Modellname), denn genau
diese Unterscheidung ist der Zweck des Commands.

Wichtig fuers Mocken: `requests` ist ein einzelnes, gecachtes Modul — egal ob es aus
`ai_diagnose.py` oder `ai_providers.py` importiert wird, `requests.post` ist dieselbe
Funktion. Ein `patch(...)` reicht deshalb fuer alle HTTP-Aufrufe innerhalb eines Tests
(Diagnose-Proben UND den Produktions-Pfad); ein zweiter, verschachtelter Patch auf
denselben Namen wuerde den ersten fuer die Dauer des `with`-Blocks nur verdecken.
"""

from io import StringIO
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.test import SimpleTestCase, override_settings

# Bewusst lang genug, dass kein Teilstring zufaellig mit einem Wort im Ausgabetext
# kollidiert (ein einzelnes "k" als Test-Key wuerde jedes "k" in "direkte" etc.
# durch [REDACTED] ersetzen und den Test an der falschen Stelle scheitern lassen).
FAKE_GEMINI_KEY = "gm-fake-9f8a7b6c5d4e3f2a1b0c"
FAKE_GROQ_KEY = "gq-fake-1a2b3c4d5e6f7a8b9c0d"


def _response(status_code: int, payload: dict):
    resp = Mock()
    resp.status_code = status_code
    resp.json.return_value = payload
    resp.text = str(payload)
    return resp


def _run_with(mock_response) -> str:
    """Fuehrt ai_diagnose aus, wobei jeder HTTP-Call dieselbe kanonische Antwort bekommt."""
    out = StringIO()
    with patch(
        "app_maintenance.management.commands.ai_diagnose.requests.post"
    ) as mock_post:
        mock_post.return_value = mock_response
        call_command("ai_diagnose", stdout=out)
    return out.getvalue()


@override_settings(GEMINI_API_KEY=FAKE_GEMINI_KEY, GROQ_API_KEY=FAKE_GROQ_KEY)
class AiDiagnoseRedactionTests(SimpleTestCase):
    """Der Key-Wert darf unter keinen Umstaenden in der Ausgabe landen."""

    def test_key_values_never_appear_in_output(self):
        output = _run_with(
            _response(200, {"candidates": [{"content": {"parts": [{"text": "OK"}]}}]})
        )
        self.assertNotIn(FAKE_GEMINI_KEY, output)
        self.assertNotIn(FAKE_GROQ_KEY, output)

    def test_redacts_key_even_inside_an_error_body(self):
        """Selbst wenn ein Provider den Key faelschlich in seiner Fehlermeldung spiegelt."""
        output = _run_with(
            _response(403, {"error": {"message": f"invalid key {FAKE_GEMINI_KEY}"}})
        )
        self.assertNotIn(FAKE_GEMINI_KEY, output)
        self.assertIn("[REDACTED]", output)


class AiDiagnoseStatusClassificationTests(SimpleTestCase):
    """Die Ausfallarten muessen im Text unterscheidbar bleiben."""

    @override_settings(GEMINI_API_KEY="", GROQ_API_KEY="")
    def test_missing_key_is_reported_as_skipped_not_as_an_error(self):
        output = _run_with(_response(200, {}))
        self.assertIn("Gemini: uebersprungen (kein Key gesetzt)", output)
        self.assertIn("Groq: uebersprungen (kein Key gesetzt)", output)

    @override_settings(GEMINI_API_KEY=FAKE_GEMINI_KEY, GROQ_API_KEY="")
    def test_401_is_reported_as_invalid_key(self):
        output = _run_with(_response(401, {"error": "unauthorized"}))
        self.assertIn("HTTP 401", output)
        self.assertIn("ungueltig", output)

    @override_settings(GEMINI_API_KEY=FAKE_GEMINI_KEY, GROQ_API_KEY="")
    def test_429_is_reported_as_rate_limit(self):
        output = _run_with(_response(429, {"error": "rate limited"}))
        self.assertIn("HTTP 429", output)
        self.assertIn("Rate-Limit", output)

    @override_settings(GEMINI_API_KEY=FAKE_GEMINI_KEY, GROQ_API_KEY="")
    def test_404_is_reported_as_model_not_found(self):
        """Genau der Fall, der die reale Anbindung lahmgelegt hat."""
        output = _run_with(_response(404, {"error": "model not found"}))
        self.assertIn("HTTP 404", output)
        self.assertIn("nicht gefunden", output)

    @override_settings(GEMINI_API_KEY=FAKE_GEMINI_KEY, GROQ_API_KEY="")
    def test_200_is_reported_as_valid(self):
        output = _run_with(_response(200, {}))
        self.assertIn("OK (HTTP 200)", output)
        self.assertIn("gueltig", output)

    @override_settings(GEMINI_API_KEY=FAKE_GEMINI_KEY, GROQ_API_KEY="")
    def test_timeout_is_reported_distinctly_from_http_errors(self):
        import requests

        out = StringIO()
        with patch(
            "app_maintenance.management.commands.ai_diagnose.requests.post"
        ) as mock_post:
            mock_post.side_effect = requests.exceptions.Timeout("timed out")
            call_command("ai_diagnose", stdout=out)

        self.assertIn("Timeout", out.getvalue())

    @override_settings(GEMINI_API_KEY=FAKE_GEMINI_KEY, GROQ_API_KEY=FAKE_GROQ_KEY)
    def test_production_path_reports_none_when_every_probe_fails(self):
        """
        Wenn beide Provider ablehnen, muss auch der reale Codepfad
        (get_ai_provider().generate_text()) sichtbar None liefern — das ist der
        Zustand, den die App als 503 'ai_unavailable' an den Nutzer weiterreicht.
        """
        output = _run_with(_response(404, {"error": "model not found"}))
        self.assertIn("Ergebnis: None", output)
        self.assertIn("ai_unavailable", output)
