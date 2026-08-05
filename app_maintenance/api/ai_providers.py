import logging
from abc import ABC, abstractmethod

import requests
from django.conf import settings

logger = logging.getLogger("my_app_debug")

# LLM-Calls sind langsamer als der REQUEST_TIMEOUT=10 der Strava/Open-Meteo-Aufrufe.
AI_REQUEST_TIMEOUT = 20


class BaseAIProvider(ABC):
    @abstractmethod
    def generate_text(self, system_prompt: str, user_prompt: str) -> str | None:
        """Muss JEDEN Fehlerfall selbst abfangen/loggen und None zurückgeben —
        darf niemals ungefangen in den Request/Response-Zyklus werfen."""
        raise NotImplementedError


class GeminiProvider(BaseAIProvider):
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

    def generate_text(self, system_prompt: str, user_prompt: str) -> str | None:
        return None


class FallbackAIProvider(BaseAIProvider):
    """Probiert mehrere Provider der Reihe nach durch, bis einer eine Antwort liefert
    (fehlender Key, Timeout, Rate-Limit, ... -> naechster Provider)."""

    def __init__(self, providers: list[BaseAIProvider]):
        self._providers = providers

    def generate_text(self, system_prompt: str, user_prompt: str) -> str | None:
        for provider in self._providers:
            result = provider.generate_text(system_prompt, user_prompt)
            if result is not None:
                return result
        return None


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
