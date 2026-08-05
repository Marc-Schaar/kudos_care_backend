import logging

from django.db.models import Max
from django.utils import timezone

from app_maintenance.models import Bike, Component, ComponentCheck, WeatherSensitivityCoefficient

logger = logging.getLogger("my_app_debug")

# ── Formel-Konstanten ────────────────────────────────────────────────────────
# Siehe Implementierungsplan "KI-gestützte Wetter-Verschleiß-Schätzung für
# Komponenten", Abschnitt 1. Dies sind Formel-Parameter (Referenzwerte/Caps),
# keine Kategorie-Gewichtungen — letztere liegen in WeatherSensitivityCoefficient.
RAIN_REF_MM_H = 2.5
RAIN_FACTOR_CAP = 3.0
HEAT_THRESHOLD_C = 28.0
HEAT_REF_DELTA_C = 10.0
HEAT_FACTOR_CAP = 2.0
COLD_THRESHOLD_C = 5.0
COLD_REF_DELTA_C = 15.0
COLD_FACTOR_CAP = 2.0
WIND_THRESHOLD_KMH = 20.0
WIND_REF_DELTA_KMH = 20.0
WIND_FACTOR_CAP = 1.0
MAX_MULTIPLIER = 2.0


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _mean(values: list[float] | None) -> float | None:
    return sum(values) / len(values) if values else None


class WeatherWearCalculator:
    """Reine, deterministische Berechnung — keine DB-/Netzwerkzugriffe."""

    @staticmethod
    def ride_multiplier(weather_data: dict | None, coeff: WeatherSensitivityCoefficient) -> float:
        """
        Multiplikator auf die gefahrene Distanz einer einzelnen Fahrt, basierend
        auf den stündlichen Regen-/Temperatur-/Wind-Werten aus Ride.weather_data.
        Fehlen/leeren eines der drei Arrays -> neutral (1.0x), kein Teilergebnis
        aus nur einem Teil der Daten.
        """
        weather_data = weather_data or {}
        precip = _mean(weather_data.get("precipitation"))
        temp = _mean(weather_data.get("temperature_2m"))
        wind = _mean(weather_data.get("wind_speed_10m"))
        if precip is None or temp is None or wind is None:
            return 1.0

        rain_factor = _clamp(precip / RAIN_REF_MM_H, 0.0, RAIN_FACTOR_CAP)
        heat_factor = (
            _clamp((temp - HEAT_THRESHOLD_C) / HEAT_REF_DELTA_C, 0.0, HEAT_FACTOR_CAP)
            if temp > HEAT_THRESHOLD_C
            else 0.0
        )
        cold_factor = (
            _clamp((COLD_THRESHOLD_C - temp) / COLD_REF_DELTA_C, 0.0, COLD_FACTOR_CAP)
            if temp < COLD_THRESHOLD_C
            else 0.0
        )
        wind_factor = (
            _clamp((wind - WIND_THRESHOLD_KMH) / WIND_REF_DELTA_KMH, 0.0, WIND_FACTOR_CAP)
            if wind > WIND_THRESHOLD_KMH
            else 0.0
        )

        raw = (
            1.0
            + coeff.rain_sensitivity * rain_factor
            + coeff.heat_sensitivity * heat_factor
            + coeff.cold_sensitivity * cold_factor
            + coeff.wind_sensitivity * wind_factor
        )
        return _clamp(raw, 1.0, MAX_MULTIPLIER)


class WeatherWearService:
    """
    Orchestriert Neuberechnung/Persistierung von Component.weather_wear_km.

    v1-Einschränkung: nur aktuell montierte Komponenten. Es gibt keine FK von
    Ride zu Component/ComponentSlot, daher keine punktgenaue Rekonstruktion,
    welches Teil zu einem früheren Zeitpunkt montiert war — ausgebaute/
    historische Komponenten werden bewusst nicht unterstützt.

    Voller Recompute, kein Delta/Watermark — jede Neuberechnung geht die
    komplette Ride-Historie seit Einbau durch (analog recompute_wind.py).
    """

    @staticmethod
    def calculate_weather_wear_km(component: Component) -> tuple[float, int]:
        """Reine Berechnung (liest DB, schreibt nicht). Gibt (weather_wear_km, ride_count) zurück."""
        from app_dashboard.models import Ride

        if not component.installed_at:
            return 0.0, 0

        coeff = WeatherSensitivityCoefficient.objects.filter(
            category=component.slot.template.category
        ).first()
        if coeff is None:
            logger.warning(
                "Keine WeatherSensitivityCoefficient fuer Kategorie %s (Komponente %s).",
                component.slot.template.category,
                component.pk,
            )
            return 0.0, 0

        rides = Ride.objects.filter(
            bike_id=component.slot.bike_id,
            start_date__date__gte=component.installed_at,
            distance__isnull=False,
        ).only("distance", "weather_data")

        total_km = 0.0
        ride_count = 0
        for ride in rides:
            distance_km = ride.distance / 1000
            multiplier = (
                WeatherWearCalculator.ride_multiplier(ride.weather_data, coeff)
                if ride.weather_data
                else 1.0
            )
            total_km += distance_km * multiplier
            ride_count += 1

        return round(total_km, 1), ride_count

    @staticmethod
    def recompute_component(component: Component) -> Component:
        weather_wear_km, ride_count = WeatherWearService.calculate_weather_wear_km(component)
        component.weather_wear_km = weather_wear_km
        component.weather_wear_ride_count = ride_count
        component.weather_wear_computed_at = timezone.now()
        component.save(
            update_fields=[
                "weather_wear_km",
                "weather_wear_ride_count",
                "weather_wear_computed_at",
            ]
        )
        return component

    @staticmethod
    def recompute_bike(bike: Bike) -> int:
        """Rechnet ALLE aktuell montierten Komponenten des Bikes neu."""
        components = Component.objects.filter(slot__bike=bike, is_mounted=True).select_related(
            "slot__template"
        )
        count = 0
        for component in components:
            try:
                WeatherWearService.recompute_component(component)
                count += 1
            except Exception:
                logger.exception(
                    "Wetter-Verschleiss-Neuberechnung fuer Komponente %s (Bike %s) fehlgeschlagen.",
                    component.pk,
                    bike.pk,
                )
        return count


def build_weather_explanation_prompt(component: Component, wear: dict) -> tuple[str, str]:
    """Baut (system_prompt, user_prompt) fuer die KI-Erklaerung."""
    template = component.slot.template
    multiplier_pct = None
    if wear.get("wear_km") and wear["wear_km"] > 0 and component.weather_wear_km is not None:
        multiplier_pct = round((component.weather_wear_km / wear["wear_km"] - 1) * 100)

    system_prompt = (
        "Du bist ein Assistent für eine Fahrrad-Wartungs-App. Du bekommst bereits fertig "
        "berechnete Kennzahlen zum wetterbedingten Verschleiß einer Fahrradkomponente. "
        "Erkläre dem Nutzer in 2-3 kurzen, allgemeinverständlichen Sätzen auf Deutsch, WARUM "
        "der wetterbereinigte Verschleiß höher ist als der reine Kilometerstand vermuten "
        "lässt. Nutze ausschließlich die gegebenen Zahlen — führe KEINE eigenen Berechnungen "
        "durch und erfinde KEINE zusätzlichen Werte oder Statistiken. Halte den Ton sachlich "
        "und konkret (z.B. 'Regen' statt 'widrige Bedingungen'). Antworte nur mit dem "
        "Erklärtext, ohne Einleitung, ohne Anführungszeichen."
    )
    user_prompt = (
        f"Komponente: {template.name} ({template.get_category_display()})\n"
        f"Roh-Kilometerstand seit Einbau: {wear.get('wear_km')} km\n"
        f"Wetterbereinigter Verschleiß: {component.weather_wear_km} km\n"
        f"Mehrverschleiß durch Wetter: "
        f"{multiplier_pct if multiplier_pct is not None else 'unbekannt'}%\n"
        f"Anzahl berücksichtigter Fahrten: {component.weather_wear_ride_count}\n"
        f"Empfohlene Lebensdauer: {component.effective_warn_km} km\n"
        f"Aktueller wetterbereinigter Status: {wear.get('warn_status_weather_km')}\n"
    )
    return system_prompt, user_prompt


def build_check_instructions_prompt(component: Component, wear: dict) -> tuple[str, str]:
    """
    Baut (system_prompt, user_prompt) fuer eine KI-Anleitung, WIE der Nutzer die
    Komponente selbst am besten prueft. Anders als build_weather_explanation_prompt
    (erklaert bereits berechnete Zahlen) geht es hier um praktische Handlungsschritte —
    die KI bekommt den aktuellen Verschleiss-Status nur als Kontext fuer die
    Dringlichkeit, erfindet aber keine eigenen Zahlen.
    """
    template = component.slot.template

    system_prompt = (
        "Du bist ein Assistent für eine Fahrrad-Wartungs-App. Ein Nutzer möchte wissen, "
        "wie er eine bestimmte Fahrradkomponente selbst prüfen kann. Gib eine kurze, "
        "praktische Schritt-für-Schritt-Anleitung auf Deutsch (3-6 Punkte als Liste mit "
        "Bindestrichen), worauf beim Prüfen zu achten ist, welche einfachen Hilfsmittel "
        "ggf. nötig sind, und welche Anzeichen für einen notwendigen Austausch sprechen. "
        "Nutze den angegebenen Verschleiß-Status nur, um die Dringlichkeit einzuordnen — "
        "erfinde KEINE eigenen Zahlen oder Messwerte. Weise bei sicherheitskritischen "
        "Komponenten (z.B. Bremsen, Federung, Rahmen) klar darauf hin, dass im Zweifel "
        "eine Fachwerkstatt die Prüfung übernehmen sollte. Antworte nur mit der Anleitung, "
        "ohne Einleitung, ohne Anführungszeichen."
    )
    user_prompt = (
        f"Komponente: {template.name} ({template.get_category_display()})\n"
        f"Verschleiß seit Einbau: {wear.get('wear_km')} km / {wear.get('wear_days')} Tage\n"
        f"Wetterbereinigter Verschleiß: {component.weather_wear_km} km\n"
        f"Empfohlene Lebensdauer: {component.effective_warn_km} km / "
        f"{component.effective_warn_days} Tage\n"
        f"Aktueller Gesamt-Status: {wear.get('warn_status_overall')}\n"
    )
    return system_prompt, user_prompt


def bike_condition_report_is_stale(bike: Bike) -> bool:
    """
    True wenn der gecachte Zustandsbericht noch nie generiert wurde, oder wenn
    sich seither Zahlen geändert haben könnten, die in den Bericht eingeflossen
    sind: neue Fahrten (km-Achse), eine Wetter-Verschleiß-Neuberechnung, oder
    eine neue Prüfung/Freigabe. Es gibt keinen einzelnen "computed_at"-Zeitstempel
    für den Gesamtzustand (anders als bei Component.weather_wear_computed_at),
    daher die Max-Aggregation über die drei Signalquellen.
    """
    generated_at = bike.condition_report_generated_at
    if generated_at is None:
        return True

    latest_ride = bike.rides.aggregate(latest=Max("created_at"))["latest"]
    if latest_ride and latest_ride > generated_at:
        return True

    mounted = Component.objects.filter(slot__bike=bike, is_mounted=True)
    latest_weather = mounted.aggregate(latest=Max("weather_wear_computed_at"))["latest"]
    if latest_weather and latest_weather > generated_at:
        return True

    latest_check = ComponentCheck.objects.filter(component__slot__bike=bike).aggregate(
        latest=Max("created_at")
    )["latest"]
    if latest_check and latest_check > generated_at:
        return True

    return False


def build_bike_condition_report_prompt(bike: Bike, component_summaries: list[dict]) -> tuple[str, str]:
    """
    Baut (system_prompt, user_prompt) fuer den Gesamt-Zustandsbericht eines Bikes.
    `component_summaries` enthält je montierter Komponente Name/Kategorie plus das
    compute_wear()-Ergebnis (siehe BikeConditionReportView) — die KI berechnet nichts
    selbst, sie fasst nur die bereits berechneten Werte in Worten zusammen.
    """
    system_prompt = (
        "Du bist ein Assistent für eine Fahrrad-Wartungs-App. Du bekommst bereits fertig "
        "berechnete Verschleiß-Kennzahlen aller aktuell montierten Komponenten eines "
        "Fahrrads. Fasse den Gesamtzustand in 3-5 kurzen, allgemeinverständlichen Sätzen "
        "auf Deutsch zusammen: welche Komponenten kritisch oder bald fällig sind und "
        "welche unproblematisch sind. Nutze ausschließlich die gegebenen Zahlen — führe "
        "KEINE eigenen Berechnungen durch und erfinde KEINE zusätzlichen Werte oder "
        "Komponenten. Halte den Ton sachlich und konkret. Antworte nur mit dem Text, "
        "ohne Einleitung, ohne Anführungszeichen."
    )

    lines = [
        f"Fahrrad: {bike.name} ({bike.get_bike_type_display()})",
        f"Gesamtkilometer: {bike.total_distance_km}",
        "Komponenten:",
    ]
    for summary in component_summaries:
        lines.append(
            f"- {summary['name']} ({summary['category']}): "
            f"{summary['wear_km']} km / {summary['wear_days']} Tage seit Einbau, "
            f"wetterbereinigt {summary['weather_wear_km']} km, "
            f"Status: {summary['warn_status_overall']}"
        )
    user_prompt = "\n".join(lines)
    return system_prompt, user_prompt
