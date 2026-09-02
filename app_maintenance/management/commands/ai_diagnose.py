"""
Diagnose-Command fuer die KI-Anbindung (Gemini/Groq).

Prueft, OHNE jemals einen Key-Wert auszugeben:
  - welcher AI_PROVIDER aktiv ist und welche Keys ueberhaupt gesetzt sind
  - je gesetztem Key: eine minimale Test-Anfrage direkt an den Provider, mit
    Status-Code und (redigierter) Fehlermeldung — das unterscheidet "Key fehlt"
    von "Key ungueltig (401/403)" von "Rate-Limit (429)" von "Netzwerk/Timeout"
  - den Pfad, den die App tatsaechlich nutzt (get_ai_provider().generate_text()),
    damit sichtbar wird, ob z.B. der Fallback Gemini->Groq nur einen der beiden
    Fehler verdeckt

Gemini nutzt hier bewusst den Header `x-goog-api-key` statt `?key=...` in der URL
(anders als der Produktionscode in ai_providers.py) — Requests-Exceptions serialisieren
oft die vollstaendige URL, und die soll in keinem Log/Actions-Run auftauchen. Zusaetzlich
wird jede Ausgabe hart gegen die bekannten Key-Werte redigiert, als zweite Absicherung.
"""

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

REQUEST_TIMEOUT = 15


def _redact(text: str, secrets: list[str]) -> str:
    """Ersetzt jedes bekannte Secret durch [REDACTED], egal wo es auftaucht."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


class Command(BaseCommand):
    help = (
        "Prueft die Gemini/Groq-Anbindung (Key gesetzt? gueltig? Rate-Limit? "
        "Netzwerk?) ohne jemals einen Key-Wert auszugeben."
    )

    def handle(self, *args, **options):
        secrets = [s for s in (settings.GEMINI_API_KEY, settings.GROQ_API_KEY) if s]

        def out(line: str):
            self.stdout.write(_redact(line, secrets))

        out(f"AI_PROVIDER = {settings.AI_PROVIDER!r}")
        out(
            f"GEMINI_API_KEY gesetzt: {bool(settings.GEMINI_API_KEY)} "
            f"(Laenge {len(settings.GEMINI_API_KEY or '')}), "
            f"Modell: {settings.GEMINI_MODEL!r}"
        )
        out(
            f"GROQ_API_KEY gesetzt:   {bool(settings.GROQ_API_KEY)} "
            f"(Laenge {len(settings.GROQ_API_KEY or '')}), "
            f"Modell: {settings.GROQ_MODEL!r}"
        )
        out("")

        if settings.GEMINI_API_KEY:
            out("--- Gemini: direkte Testanfrage ---")
            out(self._probe_gemini())
        else:
            out("--- Gemini: uebersprungen (kein Key gesetzt) ---")
        out("")

        if settings.GROQ_API_KEY:
            out("--- Groq: direkte Testanfrage ---")
            out(self._probe_groq())
        else:
            out("--- Groq: uebersprungen (kein Key gesetzt) ---")
        out("")

        out("--- Produktions-Pfad: get_ai_provider().generate_text() ---")
        from app_maintenance.api.ai_providers import get_ai_provider

        provider = get_ai_provider()
        text, source = provider.generate_text_with_source(
            "Antworte nur mit dem Wort OK.", "Testanfrage fuer Diagnose."
        )
        if text is None:
            out(
                "Ergebnis: None — die App bekaeme jetzt ueberall den "
                "503 'ai_unavailable'-Pfad. Details siehe die Proben oben."
            )
        else:
            out(f"Ergebnis: OK, Provider={source!r}, Antwortlaenge={len(text)}")

    def _probe_gemini(self) -> str:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{settings.GEMINI_MODEL}:generateContent"
        )
        headers = {"x-goog-api-key": settings.GEMINI_API_KEY}
        payload = {
            "contents": [{"role": "user", "parts": [{"text": "Antworte nur mit OK."}]}],
            "generationConfig": {"maxOutputTokens": 10},
        }
        return self._probe(url, headers, payload)

    def _probe_groq(self) -> str:
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {settings.GROQ_API_KEY}"}
        payload = {
            "model": settings.GROQ_MODEL,
            "messages": [{"role": "user", "content": "Antworte nur mit OK."}],
            "max_tokens": 10,
        }
        return self._probe(url, headers, payload)

    def _probe(self, url: str, headers: dict, payload: dict) -> str:
        try:
            resp = requests.post(
                url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT
            )
        except requests.exceptions.Timeout:
            return f"FEHLER: Timeout nach {REQUEST_TIMEOUT}s — Netzwerk/Firewall vom Server aus?"
        except requests.exceptions.ConnectionError as e:
            return f"FEHLER: Verbindung fehlgeschlagen ({type(e).__name__}) — DNS/Firewall?"
        except requests.exceptions.RequestException as e:
            return f"FEHLER: {type(e).__name__}"

        status = resp.status_code
        if status == 200:
            return "OK (HTTP 200) — Key ist gueltig und aktiv."
        if status in (401, 403):
            body = self._short_body(resp)
            return f"FEHLER: HTTP {status} (Key ungueltig/nicht autorisiert). Antwort: {body}"
        if status == 429:
            body = self._short_body(resp)
            return f"FEHLER: HTTP 429 (Rate-Limit/Kontingent aufgebraucht). Antwort: {body}"
        if status == 404:
            body = self._short_body(resp)
            model = (
                settings.GEMINI_MODEL
                if "generativelanguage" in url
                else settings.GROQ_MODEL
            )
            return f"FEHLER: HTTP 404 (Modell {model!r} nicht gefunden/umbenannt?). Antwort: {body}"
        body = self._short_body(resp)
        return f"FEHLER: HTTP {status}. Antwort: {body}"

    @staticmethod
    def _short_body(resp) -> str:
        try:
            data = resp.json()
        except ValueError:
            return resp.text[:300]
        text = str(data)
        return text[:300]
