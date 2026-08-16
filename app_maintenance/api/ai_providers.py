import logging
from abc import ABC, abstractmethod

import requests
from django.conf import settings

logger = logging.getLogger("my_app_debug")

# LLM-Calls sind langsamer als der REQUEST_TIMEOUT=10 der Strava/Open-Meteo-Aufrufe.
AI_REQUEST_TIMEOUT = 20


class BaseAIProvider(ABC):
    name: str = "unknown"

    @abstractmethod
    def generate_text(self, system_prompt: str, user_prompt: str) -> str | None:
        """Muss JEDEN Fehlerfall selbst abfangen/loggen und None zurückgeben —
        darf niemals ungefangen in den Request/Response-Zyklus werfen."""
        raise NotImplementedError

    def generate_text_with_source(self, system_prompt: str, user_prompt: str) -> tuple[str | None, str | None]:
        """Wie generate_text(), gibt zusätzlich zurück, welcher konkrete Provider
        (self.name) die Antwort erzeugt hat — genutzt von generate_reviewed_text(),
        um das Gegenstück (Gemini<->Groq) für die Zweit-Prüfung zu bestimmen."""
        text = self.generate_text(system_prompt, user_prompt)
        return text, (self.name if text is not None else None)


class GeminiProvider(BaseAIProvider):
    name = "gemini"

    def generate_text(self, system_prompt: str, user_prompt: str) -> str | None:
        api_key = settings.GEMINI_API_KEY
        if not api_key:
            logger.warning("GEMINI_API_KEY fehlt, keine KI-Erklaerung moeglich.")
            return None

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{settings.GEMINI_MODEL}:generateContent?key={api_key}"
        )
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {"temperature": 0.4, "maxOutputTokens": 300},
        }
        try:
            resp = requests.post(url, json=payload, timeout=AI_REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except requests.exceptions.RequestException as e:
            logger.error("Gemini-Anfrage fehlgeschlagen: %s", e)
            return None
        except (KeyError, IndexError, TypeError, ValueError) as e:
            logger.error("Gemini-Antwort hatte unerwartetes Format: %s", e)
            return None


class GroqProvider(BaseAIProvider):
    name = "groq"

    def generate_text(self, system_prompt: str, user_prompt: str) -> str | None:
        api_key = settings.GROQ_API_KEY
        if not api_key:
            logger.warning("GROQ_API_KEY fehlt, keine KI-Erklaerung moeglich.")
            return None

        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}"}
        payload = {
            "model": settings.GROQ_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.4,
            "max_tokens": 300,
        }
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=AI_REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
        except requests.exceptions.RequestException as e:
            logger.error("Groq-Anfrage fehlgeschlagen: %s", e)
            return None
        except (KeyError, IndexError, TypeError, ValueError) as e:
            logger.error("Groq-Antwort hatte unerwartetes Format: %s", e)
            return None


class NullAIProvider(BaseAIProvider):
    """Fallback wenn AI_PROVIDER nicht gesetzt/erkannt ist."""

    name = "none"

    def generate_text(self, system_prompt: str, user_prompt: str) -> str | None:
        return None


class FallbackAIProvider(BaseAIProvider):
    """Probiert mehrere Provider der Reihe nach durch, bis einer eine Antwort liefert
    (fehlender Key, Timeout, Rate-Limit, ... -> naechster Provider)."""

    name = "fallback"

    def __init__(self, providers: list[BaseAIProvider]):
        self._providers = providers

    def generate_text(self, system_prompt: str, user_prompt: str) -> str | None:
        text, _ = self.generate_text_with_source(system_prompt, user_prompt)
        return text

    def generate_text_with_source(self, system_prompt: str, user_prompt: str) -> tuple[str | None, str | None]:
        for provider in self._providers:
            result = provider.generate_text(system_prompt, user_prompt)
            if result is not None:
                return result, provider.name
        return None, None


def get_ai_provider() -> BaseAIProvider:
    provider = (settings.AI_PROVIDER or "").lower()
    if provider == "gemini":
        # Guenstiges/schnelles Flash-Lite-Modell (settings.GEMINI_MODEL) als primaerer
        # Provider, bei Fehlschlag (Key/Timeout/Rate-Limit/...) Fallback auf Groq.
        return FallbackAIProvider([GeminiProvider(), GroqProvider()])
    if provider == "groq":
        return GroqProvider()
    logger.warning("Unbekannter/fehlender AI_PROVIDER=%r, KI-Erklaerungen deaktiviert.", provider)
    return NullAIProvider()


def _get_review_counterpart(generator_name: str | None) -> BaseAIProvider | None:
    """Gibt das jeweils andere Gemini/Groq-Provider-Modell zurueck, das eine vom
    `generator_name`-Provider erzeugte Antwort gegenpruefen soll. None wenn kein
    Gegenstueck verfuegbar ist (fehlender Key) — dann wird ungeprueft ausgeliefert."""
    if generator_name == "gemini":
        return GroqProvider() if settings.GROQ_API_KEY else None
    if generator_name == "groq":
        return GeminiProvider() if settings.GEMINI_API_KEY else None
    return None


def _review_passes(reviewer: BaseAIProvider, user_prompt: str, answer: str) -> bool:
    """Laesst `reviewer` pruefen, ob `answer` inhaltlich sinnvoll ist und nicht den
    gegebenen Ausgangsdaten widerspricht/Werte erfindet. Fail-open: schlaegt die
    Pruef-Anfrage selbst fehl (Key/Timeout/unerwartetes Format), wird nicht blockiert —
    nur eine eindeutige 'FEHLER'-Antwort des Reviewers gilt als durchgefallen."""
    review_system_prompt = (
        "Du bist ein Qualitaets-Pruefer fuer KI-generierte Texte einer "
        "Fahrrad-Wartungs-App. Du bekommst Ausgangsdaten und einen von einer anderen "
        "KI daraus generierten Antworttext. Pruefe NUR zwei Dinge: (1) Ist der Text "
        "inhaltlich sinnvoll und verstaendlich? (2) Widerspricht der Text den "
        "gegebenen Zahlen/Fakten oder erfindet er Werte, die nicht in den "
        "Ausgangsdaten stehen? Antworte ausschliesslich mit 'OK', wenn der Text beide "
        "Kriterien erfuellt, sonst mit 'FEHLER: <kurzer Grund>'."
    )
    review_user_prompt = f"Ausgangsdaten:\n{user_prompt}\n\nZu pruefender Text:\n{answer}"

    verdict = reviewer.generate_text(review_system_prompt, review_user_prompt)
    if verdict is None:
        return True
    return verdict.strip().upper().startswith("OK")


def generate_reviewed_text(system_prompt: str, user_prompt: str) -> str | None:
    """
    Wie get_ai_provider().generate_text(), zusaetzlich mit einer Zweit-Pruefung der
    Antwort auf Sinnhaftigkeit/Konsistenz mit den Ausgangsdaten durch das jeweils
    andere Gemini/Groq-Modell (Gemini generiert -> Groq prueft, und umgekehrt).
    Faellt die Pruefung durch, wird EINMAL neu generiert und die zweite Antwort
    ungeprueft ausgeliefert (kein zweiter Pruef-Roundtrip, um die Latenz fuer den
    Nutzer nicht zu verdoppeln) — schlaegt auch die Neugenerierung fehl, wird die
    urspruengliche (nicht bestandene) Antwort ausgeliefert statt None, denn ein
    inhaltlicher Zweifel ist kein Grund fuer ein hartes 503 wie ein Provider-Ausfall.
    Ist kein Gegenstueck-Provider verfuegbar (fehlender Key), wird fail-open
    ungeprueft ausgeliefert.
    """
    provider = get_ai_provider()
    text, source = provider.generate_text_with_source(system_prompt, user_prompt)
    if text is None:
        return None

    reviewer = _get_review_counterpart(source)
    if reviewer is None:
        return text

    if _review_passes(reviewer, user_prompt, text):
        return text

    logger.warning("KI-Antwort von %s bestand Zweit-Pruefung nicht, generiere einmal neu.", source)
    retry_text, _ = provider.generate_text_with_source(system_prompt, user_prompt)
    return retry_text if retry_text is not None else text
