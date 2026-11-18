"""
Einwilligungen in die Weitergabe von Nutzerdaten an Dritte.

Stravas API Agreement verbietet, Daten eines Nutzers ohne ausdrückliche Einwilligung an
Dritte zu geben. Betroffen sind drei Dinge, die sich getrennt schalten lassen:

- Wetter: Startkoordinaten und Datum jeder Fahrt gehen an Open-Meteo.
- Regen entlang der Strecke: zusätzlich auf rund 1 km gerundete Streckenpunkte an Open-Meteo.
  Baut auf der Wetter-Einwilligung auf und endet mit ihr. Eine GPS-Strecke verrät mehr als ein
  Startpunkt, deshalb gilt die Wetter-Einwilligung allein dafür nicht.
- KI: Fahrtname, Datum, Kennzahlen, Rad- und Teilenamen gehen an Gemini oder Groq.

Die Prüfung gehört direkt vor den Aufruf des Anbieters, nicht an den Anfang der View: eine
gecachte Antwort überträgt nichts neu und bleibt deshalb auch ohne Einwilligung lesbar.
"""

from rest_framework import status
from rest_framework.response import Response

from app_auth.models import StravaProfile

AI_CONSENT_REQUIRED_CODE = "ai_consent_required"


def ai_consent_required_response() -> Response:
    return Response(
        {
            "error": "Für KI-Texte brauchen wir erst deine Einwilligung.",
            "code": AI_CONSENT_REQUIRED_CODE,
        },
        status=status.HTTP_403_FORBIDDEN,
    )


def has_ai_consent(profile: StravaProfile) -> bool:
    return profile.ai_consent_at is not None


def has_weather_consent(profile: StravaProfile) -> bool:
    return profile.weather_consent_at is not None


def has_route_weather_consent(profile: StravaProfile) -> bool:
    return has_weather_consent(profile) and (
        profile.route_weather_consent_at is not None
    )
