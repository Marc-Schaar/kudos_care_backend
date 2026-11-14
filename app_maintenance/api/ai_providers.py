import json
import logging
import re
from abc import ABC, abstractmethod

import requests
from django.conf import settings

logger = logging.getLogger("my_app_debug")

# LLM-Calls sind langsamer als der REQUEST_TIMEOUT=10 der Strava/Open-Meteo-Aufrufe.
AI_REQUEST_TIMEOUT = 20

# Ein Recherche-Aufruf schlaegt erst Suchtreffer nach und liest sie, bevor er
# antwortet — das dauert deutlich laenger als eine reine Modellantwort.
AI_RESEARCH_TIMEOUT = 45

# Freitext-Antworten sind kurz (2-5 Saetze). Ein komplettes Bike-Setup als JSON hat
# dagegen leicht 30+ Zeilen — mit 300 Tokens bricht die Antwort mitten im JSON ab.
AI_MAX_OUTPUT_TOKENS = 300
AI_JSON_MAX_OUTPUT_TOKENS = 4000

# LLMs verpacken JSON gern in ```json ... ``` trotz gegenteiliger Anweisung.
_JSON_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _parse_json_response(raw: str | None, provider_name: str) -> dict | None:
    """
    Parst eine JSON-Antwort robust: entfernt ```json-Fences und akzeptiert nur ein
    Objekt an oberster Ebene. Gibt None statt zu werfen — dieselbe Zusicherung wie
    `BaseAIProvider.generate_text()`.
    """
    if not raw:
        return None
    cleaned = _JSON_FENCE_RE.sub("", raw).strip()
    try:
        parsed = json.loads(cleaned)
    except (ValueError, TypeError) as e:
        logger.error("%s lieferte kein gueltiges JSON: %s", provider_name, e)
        return None
    if not isinstance(parsed, dict):
        logger.error(
            "%s lieferte JSON, aber kein Objekt (%s).", provider_name, type(parsed)
        )
        return None
    return parsed


class BaseAIProvider(ABC):
    name: str = "unknown"

    @abstractmethod
    def generate_text(self, system_prompt: str, user_prompt: str) -> str | None:
        """Muss JEDEN Fehlerfall selbst abfangen/loggen und None zurückgeben —
        darf niemals ungefangen in den Request/Response-Zyklus werfen."""
        raise NotImplementedError

    def generate_text_with_source(
        self, system_prompt: str, user_prompt: str
    ) -> tuple[str | None, str | None]:
        """Wie generate_text(), gibt zusätzlich zurück, welcher konkrete Provider
        (self.name) die Antwort erzeugt hat — genutzt von generate_reviewed_text(),
        um das Gegenstück (Gemini<->Groq) für die Zweit-Prüfung zu bestimmen."""
        text = self.generate_text(system_prompt, user_prompt)
        return text, (self.name if text is not None else None)

    def generate_json(self, system_prompt: str, user_prompt: str) -> dict | None:
        """
        Wie generate_text(), erzwingt aber eine JSON-Antwort und gibt sie geparst
        zurück. Gleicher Vertrag: fängt jeden Fehlerfall selbst ab (inkl. kaputtem
        JSON) und gibt None zurück, statt zu werfen.

        Default-Implementierung für Provider ohne nativen JSON-Modus.
        """
        raw = self.generate_text(system_prompt, user_prompt)
        return _parse_json_response(raw, self.name)

    def generate_json_researched(
        self, system_prompt: str, user_prompt: str
    ) -> dict | None:
        """
        Wie `generate_json()`, darf die Frage aber im Web recherchieren, statt sie
        allein aus dem Modellwissen zu beantworten.

        Gedacht für Fragen nach realen Produktdaten (welches Modell gab es
        wirklich, was war ab Werk verbaut) — dort ist eine erfundene Antwort
        besonders teuer, weil sie plausibel aussieht.

        Provider ohne Recherche-Fähigkeit fallen hier auf `generate_json()`
        zurück; der Aufrufer bekommt also immer eine Antwort, nur ggf. eine
        ungeprüfte. Ob tatsächlich recherchiert wurde, sagt
        `last_call_was_researched`.
        """
        return self.generate_json(system_prompt, user_prompt)

    #: Ob der letzte `generate_json_researched()`-Aufruf wirklich im Web recherchiert
    #: hat. False heisst "aus dem Modellwissen beantwortet" — der Aufrufer kann das
    #: an die UI durchreichen, statt Recherche zu suggerieren, die nicht stattfand.
    last_call_was_researched: bool = False


class GeminiProvider(BaseAIProvider):
    name = "gemini"

    def _request(
        self,
        system_prompt: str,
        user_prompt: str,
        generation_config: dict,
        tools: list | None = None,
        timeout: int = AI_REQUEST_TIMEOUT,
    ) -> str | None:
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
            "generationConfig": generation_config,
        }
        if tools:
            payload["tools"] = tools
        try:
            resp = requests.post(url, json=payload, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            candidate = data["candidates"][0]
            # Bei aktivierter Websuche verteilt Gemini die Antwort auf mehrere
            # Parts; nur den ersten zu nehmen schneidet sie mittendrin ab.
            parts = candidate["content"]["parts"]
            text = "".join(part.get("text", "") for part in parts).strip()
            self.last_call_was_researched = bool(candidate.get("groundingMetadata"))
            return text or None
        except requests.exceptions.RequestException as e:
            logger.error("Gemini-Anfrage fehlgeschlagen: %s", e)
            return None
        except (KeyError, IndexError, TypeError, ValueError) as e:
            logger.error("Gemini-Antwort hatte unerwartetes Format: %s", e)
            return None

    def generate_text(self, system_prompt: str, user_prompt: str) -> str | None:
        return self._request(
            system_prompt,
            user_prompt,
            {"temperature": 0.4, "maxOutputTokens": AI_MAX_OUTPUT_TOKENS},
        )

    def generate_json(self, system_prompt: str, user_prompt: str) -> dict | None:
        self.last_call_was_researched = False
        raw = self._request(
            system_prompt,
            user_prompt,
            {
                "temperature": 0.2,
                "maxOutputTokens": AI_JSON_MAX_OUTPUT_TOKENS,
                "responseMimeType": "application/json",
            },
        )
        return _parse_json_response(raw, self.name)

    def generate_json_researched(
        self, system_prompt: str, user_prompt: str
    ) -> dict | None:
        """
        JSON-Antwort mit Google-Search-Grounding, damit Produktdaten belegt statt
        erinnert sind.

        Zwei Eigenheiten, beide bewusst:

        * **Kein `responseMimeType`.** Ob der JSON-Modus mit dem Suchwerkzeug
          zusammen erlaubt ist, ist nicht zugesichert; statt darauf zu wetten,
          wird das JSON im Prompt verlangt und mit demselben Fence-Parser gelesen
          wie bei Providern ohne JSON-Modus.
        * **Automatischer Rückfall auf `generate_json()`.** Grounding ist an ein
          eigenes Kontingent gebunden und antwortet auf einem Key ohne dieses
          Kontingent mit HTTP 429 — nachgemessen auf dem aktuellen Key, während
          derselbe Aufruf ohne Werkzeug 200 liefert. Der Rückfall macht daraus
          eine ungeprüfte statt gar keiner Antwort.

        `AI_GROUNDING_ENABLED=False` überspringt den Versuch ganz, damit man sich
        auf einem Key ohne Kontingent nicht bei jedem Aufruf einen 429 einhandelt.
        """
        self.last_call_was_researched = False
        if not getattr(settings, "AI_GROUNDING_ENABLED", False):
            return self.generate_json(system_prompt, user_prompt)

        raw = self._request(
            system_prompt,
            user_prompt,
            {"temperature": 0.2, "maxOutputTokens": AI_JSON_MAX_OUTPUT_TOKENS},
            tools=[{"google_search": {}}],
            timeout=AI_RESEARCH_TIMEOUT,
        )
        parsed = _parse_json_response(raw, f"{self.name} (recherchiert)")
        if parsed is not None:
            return parsed

        logger.info(
            "Gemini-Recherche nicht verfuegbar, weiche auf reines Modellwissen aus."
        )
        self.last_call_was_researched = False
        return self.generate_json(system_prompt, user_prompt)


class GroqProvider(BaseAIProvider):
    name = "groq"

    def _request(
        self, system_prompt: str, user_prompt: str, extra_payload: dict
    ) -> str | None:
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
            **extra_payload,
        }
        try:
            resp = requests.post(
                url, json=payload, headers=headers, timeout=AI_REQUEST_TIMEOUT
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
        except requests.exceptions.RequestException as e:
            logger.error("Groq-Anfrage fehlgeschlagen: %s", e)
            return None
        except (KeyError, IndexError, TypeError, ValueError) as e:
            logger.error("Groq-Antwort hatte unerwartetes Format: %s", e)
            return None

    def generate_text(self, system_prompt: str, user_prompt: str) -> str | None:
        return self._request(
            system_prompt,
            user_prompt,
            {"temperature": 0.4, "max_tokens": AI_MAX_OUTPUT_TOKENS},
        )

    def generate_json(self, system_prompt: str, user_prompt: str) -> dict | None:
        raw = self._request(
            system_prompt,
            user_prompt,
            {
                "temperature": 0.2,
                "max_tokens": AI_JSON_MAX_OUTPUT_TOKENS,
                "response_format": {"type": "json_object"},
            },
        )
        return _parse_json_response(raw, self.name)


class NullAIProvider(BaseAIProvider):
    """Fallback wenn AI_PROVIDER nicht gesetzt/erkannt ist."""

    name = "none"

    def generate_text(self, system_prompt: str, user_prompt: str) -> str | None:
        return None

    def generate_json(self, system_prompt: str, user_prompt: str) -> dict | None:
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

    def generate_text_with_source(
        self, system_prompt: str, user_prompt: str
    ) -> tuple[str | None, str | None]:
        for provider in self._providers:
            result = provider.generate_text(system_prompt, user_prompt)
            if result is not None:
                return result, provider.name
        return None, None

    def generate_json(self, system_prompt: str, user_prompt: str) -> dict | None:
        # Auch unparsebares JSON gilt als Fehlschlag und laesst den naechsten Provider
        # ran — eine halbe Antwort ist hier so unbrauchbar wie gar keine.
        for provider in self._providers:
            result = provider.generate_json(system_prompt, user_prompt)
            if result is not None:
                self.last_call_was_researched = False
                return result
        return None

    def generate_json_researched(
        self, system_prompt: str, user_prompt: str
    ) -> dict | None:
        """
        Reicht die Recherche an die Kette durch. Ein Provider ohne
        Recherche-Faehigkeit (Groq) beantwortet die Frage aus dem Modellwissen —
        immer noch besser als gar keine Antwort, aber `last_call_was_researched`
        sagt dann False.
        """
        for provider in self._providers:
            result = provider.generate_json_researched(system_prompt, user_prompt)
            if result is not None:
                self.last_call_was_researched = provider.last_call_was_researched
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
    logger.warning(
        "Unbekannter/fehlender AI_PROVIDER=%r, KI-Erklaerungen deaktiviert.", provider
    )
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
    review_user_prompt = (
        f"Ausgangsdaten:\n{user_prompt}\n\nZu pruefender Text:\n{answer}"
    )

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

    logger.warning(
        "KI-Antwort von %s bestand Zweit-Pruefung nicht, generiere einmal neu.", source
    )
    retry_text, _ = provider.generate_text_with_source(system_prompt, user_prompt)
    return retry_text if retry_text is not None else text
