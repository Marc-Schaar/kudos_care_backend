import logging
from collections.abc import Iterable
from datetime import date

from django.db.models import Max, Q
from django.utils import timezone

from app_auth.models import StravaProfile
from app_maintenance.models import (
    Bike,
    Component,
    ComponentCategory,
    ComponentCheck,
    ComponentSlot,
    MaintenanceInterval,
    WeatherSensitivityCoefficient,
)

from ..models import WarnLevel
from .serializers import compute_wear
from .usage import RideLedger, component_windows

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


DRIVER_LABELS = {
    "rain": "Regen",
    "heat": "Hitze",
    "cold": "Kälte",
    "wind": "Wind",
}


class WeatherWearCalculator:
    """Reine, deterministische Berechnung — keine DB-/Netzwerkzugriffe."""

    @staticmethod
    def ride_multiplier_detail(
        weather_data: dict | None, coeff: WeatherSensitivityCoefficient
    ) -> dict:
        """
        Wie `ride_multiplier()`, gibt zusätzlich die Einzelbeiträge zurück — Basis für
        die Anzeige "diese Fahrt hat X % mehr Bremsbelag gekostet, hauptsächlich wegen
        Regen" (siehe `ride_wear_breakdown`).

        `contributions` sind die Beiträge VOR dem Cap: greift `MAX_MULTIPLIER`, ist ihre
        Summe größer als `multiplier - 1`. Sie taugen daher zur Frage "welche Bedingung
        hat dominiert", nicht als exakte Zerlegung des Endergebnisses.
        """
        weather_data = weather_data or {}
        return WeatherWearCalculator.conditions_multiplier_detail(
            _mean(weather_data.get("precipitation")),
            _mean(weather_data.get("temperature_2m")),
            _mean(weather_data.get("wind_speed_10m")),
            coeff,
        )

    @staticmethod
    def conditions_multiplier_detail(
        precip: float | None,
        temp: float | None,
        wind: float | None,
        coeff: WeatherSensitivityCoefficient,
    ) -> dict:
        """
        Dieselbe Formel für einzelne Werte statt Stundenreihen. Gebraucht für die
        Verteilung des Fahrt-Aufschlags auf die Abschnitte (`attach_segment_wear`), wo
        jeder Abschnitt sein eigenes interpoliertes Wetter hat.
        """
        neutral = {
            "multiplier": 1.0,
            "contributions": {"rain": 0.0, "heat": 0.0, "cold": 0.0, "wind": 0.0},
            "dominant_driver": None,
            "conditions": {
                "precipitation": precip,
                "temperature": temp,
                "wind_speed": wind,
            },
            "capped": False,
        }
        if precip is None or temp is None or wind is None:
            return neutral

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
            _clamp(
                (wind - WIND_THRESHOLD_KMH) / WIND_REF_DELTA_KMH, 0.0, WIND_FACTOR_CAP
            )
            if wind > WIND_THRESHOLD_KMH
            else 0.0
        )

        contributions = {
            "rain": coeff.rain_sensitivity * rain_factor,
            "heat": coeff.heat_sensitivity * heat_factor,
            "cold": coeff.cold_sensitivity * cold_factor,
            "wind": coeff.wind_sensitivity * wind_factor,
        }
        raw = 1.0 + sum(contributions.values())
        dominant = max(contributions, key=lambda key: contributions[key])

        return {
            "multiplier": _clamp(raw, 1.0, MAX_MULTIPLIER),
            "contributions": contributions,
            "dominant_driver": dominant if contributions[dominant] > 0 else None,
            "conditions": {
                "precipitation": precip,
                "temperature": temp,
                "wind_speed": wind,
            },
            "capped": raw > MAX_MULTIPLIER,
        }

    @staticmethod
    def ride_multiplier(
        weather_data: dict | None, coeff: WeatherSensitivityCoefficient
    ) -> float:
        """
        Multiplikator auf die gefahrene Distanz einer einzelnen Fahrt, basierend
        auf den stündlichen Regen-/Temperatur-/Wind-Werten aus Ride.weather_data.
        Fehlen/leeren eines der drei Arrays -> neutral (1.0x), kein Teilergebnis
        aus nur einem Teil der Daten.
        """
        return WeatherWearCalculator.ride_multiplier_detail(weather_data, coeff)[
            "multiplier"
        ]


class WeatherWearService:
    """
    Orchestriert Neuberechnung/Persistierung von Component.weather_wear_km.

    Voller Recompute, kein Delta/Watermark — jede Neuberechnung geht alle Fahrten in
    den Nutzungszeiträumen des Teils durch (analog recompute_wind.py). Das sind
    dieselben Fahrten, die auch die km-Achse zählt (siehe api/usage.py), deshalb
    können beide Achsen nicht auseinanderlaufen.
    """

    @staticmethod
    def calculate_weather_wear_km(component: Component) -> tuple[float, int]:
        """Reine Berechnung (liest DB, schreibt nicht). Gibt (weather_wear_km, ride_count) zurück."""
        from app_dashboard.models import Ride

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

        windows = component_windows(component)
        if not windows:
            return 0.0, 0

        # Ein Q-Zweig je Nutzungszeitraum — nur Fahrten, bei denen das Teil auch
        # wirklich am Rad war. Halboffen, damit beim Tausch A → B jede Fahrt genau
        # einem Teil gehört.
        window_filter = Q()
        for start, end in windows:
            branch = Q(start_date__gte=start)
            if end is not None:
                branch &= Q(start_date__lt=end)
            window_filter |= branch

        rides = Ride.objects.filter(
            window_filter,
            bike_id=component.slot.bike_id,
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
        weather_wear_km, ride_count = WeatherWearService.calculate_weather_wear_km(
            component
        )
        # + prior_weather_km: was das Teil schon hatte, bevor es hier erfasst wurde —
        # analog zu prior_km in compute_wear().
        total = weather_wear_km + (component.prior_weather_km or 0.0)
        if (
            component.weather_wear_computed_at is not None
            and component.weather_wear_km == total
            and component.weather_wear_ride_count == ride_count
        ):
            # Keine neuen Zahlen, also auch kein neuer Zeitstempel: daran hängt die
            # Staleness der KI-Erklärung, und die kostet einen AI-Call.
            return component

        component.weather_wear_km = total
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
        """
        Rechnet alle Teile des Bikes neu, die je gefahren wurden — nicht nur die
        montierten: eine nachträglich synchronisierte oder gelöschte Fahrt kann auch
        einen Reifen betreffen, der inzwischen im Regal liegt.
        """
        components = (
            Component.objects.filter(slot__bike=bike, periods__isnull=False)
            .distinct()
            .select_related("slot__template", "slot__assembly")
            .prefetch_related("periods")
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


def ride_wear_breakdown(ride) -> dict:
    """
    Was EINE Fahrt die Komponenten gekostet hat — die fahrtbezogene Sicht auf denselben
    Multiplikator, den `WeatherWearService` sonst nur aufsummiert persistiert.

    Der Multiplikator hängt an der `ComponentCategory` (dort liegen die
    Sensitivitäts-Koeffizienten), deshalb ist die Kategorie die natürliche Gruppierung:
    alle Bremsen-Teile teilen sich denselben Prozentsatz, nur der Anteil an der jeweils
    empfohlenen Lebensdauer unterscheidet sich je Komponente.

    Berücksichtigt werden Komponenten, die **zum Zeitpunkt der Fahrt** am Rad waren —
    nicht die heute montierten: maßgeblich sind ihre Nutzungszeiträume. Nie montierte
    Ersatzteile und eine damals geparkte Baugruppe bleiben also draußen, ein seither
    abgenommener Reifen ist dabei.
    """
    if ride.bike_id is None or ride.start_date is None or not ride.distance:
        return {"distance_km": None, "categories": [], "component_count": 0}

    distance_km = ride.distance / 1000

    # Beide Bedingungen im selben filter()-Aufruf: sie müssen auf denselben
    # Nutzungszeitraum zutreffen, nicht auf zwei verschiedene.
    components = (
        Component.objects.filter(
            Q(periods__ended_at__isnull=True)
            | Q(periods__ended_at__gt=ride.start_date),
            slot__bike_id=ride.bike_id,
            periods__started_at__lte=ride.start_date,
        )
        .distinct()
        .select_related("slot__template", "slot__assembly")
    )

    coefficients = {
        coeff.category: coeff for coeff in WeatherSensitivityCoefficient.objects.all()
    }

    grouped: dict[str, dict] = {}
    component_count = 0

    for component in components:
        template = component.slot.template
        coeff = coefficients.get(template.category)
        if coeff is None:
            continue

        detail = WeatherWearCalculator.ride_multiplier_detail(ride.weather_data, coeff)
        multiplier = detail["multiplier"]
        effective_km = distance_km * multiplier
        extra_km = effective_km - distance_km
        warn_km = component.effective_warn_km

        entry = grouped.setdefault(
            template.category,
            {
                "category": template.category,
                "category_display": template.get_category_display(),
                "multiplier": round(multiplier, 3),
                "extra_pct": round((multiplier - 1) * 100),
                # Je Kategorie, nicht je Teil aufsummiert: der Aufschlag hängt am
                # Multiplikator der Kategorie, und den zahlt jedes ihrer Teile gleich.
                # Summiert stünde bei zwei Bremsteilen der doppelte Aufschlag da.
                "extra_km": round(extra_km, 2),
                "dominant_driver": detail["dominant_driver"],
                "dominant_driver_display": DRIVER_LABELS.get(detail["dominant_driver"]),
                "components": [],
            },
        )
        entry["components"].append(
            {
                "id": component.pk,
                "name": component.slot.display_name,
                "brand": component.brand,
                "model_name": component.model_name,
                "effective_km": round(effective_km, 1),
                "extra_km": round(extra_km, 2),
                "warn_km": warn_km,
                # "Diese Fahrt hat X % der empfohlenen Lebensdauer gekostet" — die
                # Zahl, nach der der Nutzer eigentlich fragt.
                "share_of_life_pct": (
                    round(effective_km / warn_km * 100, 1) if warn_km else None
                ),
            }
        )
        component_count += 1

    categories = sorted(
        grouped.values(), key=lambda entry: entry["extra_pct"], reverse=True
    )

    return {
        "distance_km": round(distance_km, 1),
        "conditions": {
            "precipitation": _round(
                _mean((ride.weather_data or {}).get("precipitation"))
            ),
            "temperature": _round(
                _mean((ride.weather_data or {}).get("temperature_2m"))
            ),
            "wind_speed": _round(
                _mean((ride.weather_data or {}).get("wind_speed_10m"))
            ),
        },
        "categories": categories,
        "component_count": component_count,
    }


def _round(value, digits: int = 1):
    return None if value is None else round(value, digits)


def attach_segment_wear(breakdown: dict, segments: list[dict]) -> dict:
    """
    Verteilt den Aufschlag jeder Kategorie auf die Windabschnitte der Fahrt
    (`app_dashboard/api/wind.py::compute_ride_wind`) und hängt ihn als
    `segment_extra_km` an: eine Liste in Abschnittsreihenfolge, gleicher Index wie
    `wind_segments.features[].properties.index`.

    **Die Summe ist der `extra_km` der Kategorie, nicht eine eigene Rechnung.** Die App
    rechnet den Verschleiß je Fahrt aus dem gemittelten Stundenwetter; dieselbe Formel
    je Abschnitt angewandt ergäbe wegen der Schwellen und Caps eine andere Summe, und
    die Seite nennte dann zwei Zahlen für dieselbe Fahrt. Das Abschnittswetter
    entscheidet deshalb nur, **wo** der Aufschlag entsteht: Gewicht je Abschnitt ist
    `Länge × (Abschnitts-Multiplikator − 1)`. Ist dieses Gewicht überall 0 (kein
    Abschnittswetter, oder das Mittel liegt über einer Schwelle, die kein Abschnitt
    einzeln reißt), wird nach Länge verteilt.
    """
    if not segments:
        for entry in breakdown.get("categories", []):
            entry["segment_extra_km"] = []
        return breakdown

    lengths = [max(0.0, s.get("distance_km") or 0.0) for s in segments]
    total_length = sum(lengths)
    coefficients = {
        coeff.category: coeff
        for coeff in WeatherSensitivityCoefficient.objects.filter(
            category__in=[e["category"] for e in breakdown.get("categories", [])]
        )
    }

    for entry in breakdown.get("categories", []):
        extra = entry.get("extra_km") or 0.0
        coeff = coefficients.get(entry["category"])
        if extra <= 0 or coeff is None or total_length <= 0:
            entry["segment_extra_km"] = [0.0] * len(segments)
            continue

        weights = []
        for segment, length in zip(segments, lengths):
            multiplier = WeatherWearCalculator.conditions_multiplier_detail(
                segment.get("precipitation"),
                segment.get("temperature"),
                segment.get("wind_speed"),
                coeff,
            )["multiplier"]
            weights.append(length * (multiplier - 1.0))

        total_weight = sum(weights)
        if total_weight <= 0:
            weights, total_weight = lengths, total_length

        entry["segment_extra_km"] = [
            round(extra * weight / total_weight, 4) for weight in weights
        ]
    return breakdown


def build_ride_wear_impact_prompt(ride, breakdown: dict) -> tuple[str, str]:
    """
    Baut (system_prompt, user_prompt) für die KI-Erklärung, was eine einzelne Fahrt die
    Komponenten gekostet hat. Wie bei den anderen Prompt-Buildern rechnet die KI nichts
    selbst — sie erzählt die bereits von `ride_wear_breakdown()` berechneten Zahlen.
    """
    system_prompt = (
        "Du bist ein Assistent für eine Fahrrad-Wartungs-App. Du bekommst bereits fertig "
        "berechnete Zahlen dazu, wie stark eine EINZELNE Fahrt die Komponenten eines "
        "Fahrrads abgenutzt hat — inklusive des wetterbedingten Aufschlags gegenüber der "
        "reinen Distanz. Erkläre dem Nutzer in 2-3 kurzen, allgemeinverständlichen Sätzen "
        "auf Deutsch, was ihn diese Fahrt gekostet hat, und nenne dabei konkret die "
        "Bedingung, die den Aufschlag verursacht hat (z.B. 'Regen' statt 'widrige "
        "Bedingungen'). Nutze ausschließlich die gegebenen Zahlen — führe KEINE eigenen "
        "Berechnungen durch und erfinde KEINE zusätzlichen Werte. Gab es keinen "
        "nennenswerten Aufschlag, sage das ruhig deutlich. Antworte nur mit dem Text, "
        "ohne Einleitung, ohne Anführungszeichen."
    )

    conditions = breakdown.get("conditions") or {}
    lines = [
        f"Fahrt: {ride.name}",
        f"Distanz: {breakdown.get('distance_km')} km",
        f"Durchschnittlicher Niederschlag: {conditions.get('precipitation')} mm/h",
        f"Durchschnittstemperatur: {conditions.get('temperature')} °C",
        f"Durchschnittliche Windgeschwindigkeit: {conditions.get('wind_speed')} km/h",
        "Betroffene Komponenten-Kategorien:",
    ]
    for category in breakdown.get("categories", []):
        driver = category.get("dominant_driver_display") or "keine besondere Bedingung"
        lines.append(
            f"- {category['category_display']}: +{category['extra_pct']}% "
            f"(+{category['extra_km']} km), Haupttreiber: {driver}"
        )
        for component in category["components"]:
            share = component["share_of_life_pct"]
            share_text = (
                f"{share}% der empfohlenen Lebensdauer"
                if share is not None
                else "unbekannt"
            )
            lines.append(f"    - {component['name']}: diese Fahrt = {share_text}")

    return system_prompt, "\n".join(lines)


def ride_wear_impact_is_stale(ride) -> bool:
    """
    True wenn die gecachte Erklärung neu generiert werden muss. Die Ride-Zahlen selbst
    sind nach dem Import unveränderlich, die Aufschlüsselung hängt aber daran, welche
    Komponenten zum Fahrtzeitpunkt montiert waren — und das ändert sich, wenn der Nutzer
    nachträglich ein Einbaudatum korrigiert oder ein Teil austauscht. Dieselbe
    Max-Aggregation über die Signalquelle wie `bike_condition_report_is_stale()`.
    """
    generated_at = ride.wear_impact_generated_at
    if generated_at is None or not ride.wear_impact_summary:
        return True
    if ride.bike_id is None:
        return False

    latest_change = Component.objects.filter(slot__bike_id=ride.bike_id).aggregate(
        latest=Max("updated_at")
    )["latest"]
    return bool(latest_change and latest_change > generated_at)


def build_weather_explanation_prompt(
    component: Component, wear: dict
) -> tuple[str, str]:
    """Baut (system_prompt, user_prompt) fuer die KI-Erklaerung."""
    template = component.slot.template
    multiplier_pct = None
    if (
        wear.get("wear_km")
        and wear["wear_km"] > 0
        and component.weather_wear_km is not None
    ):
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


def build_check_instructions_prompt(
    component: Component, wear: dict
) -> tuple[str, str]:
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

    mounted = Component.objects.mounted().filter(
        slot__in=ComponentSlot.objects.on_bike(bike)
    )
    latest_weather = mounted.aggregate(latest=Max("weather_wear_computed_at"))["latest"]
    if latest_weather and latest_weather > generated_at:
        return True

    latest_check = ComponentCheck.objects.filter(component__slot__bike=bike).aggregate(
        latest=Max("created_at")
    )["latest"]
    if latest_check and latest_check > generated_at:
        return True

    return False


def build_bike_condition_report_prompt(
    bike: Bike, component_summaries: list[dict]
) -> tuple[str, str]:
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


# ── Benachrichtigungen (siehe app_notifications) ──────────────────────────────────────────
# Fachliche "was ist los"-Logik fuer die Warn-/Vorhersage-E-Mails. app_notifications ruft
# diese Funktionen auf und kuemmert sich nur um Versand/Scheduling/Dedupe-Persistierung.


def get_new_component_warnings(
    profile: StravaProfile, bike: Bike | None = None
) -> list[dict]:
    """
    Ermittelt montierte Komponenten des Profils (optional auf ein einzelnes Bike
    eingeschraenkt), deren warn_status_overall aktuell warn/critical ist UND sich seit der
    letzten Warn-E-Mail geaendert hat (Component.last_warn_notified_status). Wird sowohl vom
    event-getriggerten Einzel-Bike-Check (nach einer Fahrt) als auch vom taeglichen
    Voll-Scan verwendet (siehe app_notifications.tasks).
    """
    bikes = Bike.objects.filter(athlete=profile, retired=False)
    if bike is not None:
        bikes = bikes.filter(pk=bike.pk)

    results = []
    for b in bikes:
        slots = (
            ComponentSlot.objects.on_bike(b)
            .select_related("template")
            .prefetch_related("components__periods", "components__checks")
        )
        ledger = RideLedger.for_bike(b.pk)
        for slot in slots:
            comp = slot.mounted_component
            if comp is None:
                continue
            wear = compute_wear(comp, ledger)
            status = wear["warn_status_overall"]
            if (
                status in (WarnLevel.WARN, WarnLevel.CRITICAL)
                and status != comp.last_warn_notified_status
            ):
                results.append(
                    {"bike": b, "slot": slot, "component": comp, "wear": wear}
                )
    return results


def get_predicted_unsafe_bikes(profile: StravaProfile) -> list[dict]:
    """
    Ermittelt Bikes des Profils, die HEUTE noch nicht kritisch sind, aber laut Fahrt-Vorhersage
    (predict_next_ride_date/predict_next_ride_km) mit der naechsten voraussichtlichen
    Fahrt kritisch werden: Tage-Achse auf das vorhergesagte Datum projiziert, km- und
    Wetter-Achse um die geschaetzte Strecke dieser Fahrt erhoeht (siehe
    compute_wear(..., as_of=..., extra_km=...)). Bikes die bereits heute kritisch sind
    werden ausgeschlossen — das deckt bereits get_new_component_warnings() ab, keine
    Doppel-Meldung.
    """
    from app_dashboard.api.services import (  # inline: Cross-App, siehe Architektur-Notiz
        predict_next_ride_date,
        predict_next_ride_km,
    )

    results = []
    bikes = Bike.objects.filter(athlete=profile, retired=False)
    for bike in bikes:
        predicted_date = predict_next_ride_date(bike)
        if predicted_date is None:
            continue
        if bike.predicted_unsafe_notified_for_date == predicted_date:
            continue
        predicted_km = predict_next_ride_km(bike) or 0.0

        slots = (
            ComponentSlot.objects.on_bike(bike)
            .select_related("template")
            .prefetch_related("components__periods", "components__checks")
        )
        mounted = [(slot, slot.mounted_component) for slot in slots]
        mounted = [(slot, comp) for slot, comp in mounted if comp is not None]
        if not mounted:
            continue

        ledger = RideLedger.for_bike(bike.pk)
        today_worst = WarnLevel.UNKNOWN
        projected_components = []
        for slot, comp in mounted:
            today_wear = compute_wear(comp, ledger)
            if today_wear["warn_status_overall"] == WarnLevel.CRITICAL:
                today_worst = WarnLevel.CRITICAL
            projected_wear = compute_wear(
                comp, ledger, as_of=predicted_date, extra_km=predicted_km
            )
            if projected_wear["warn_status_overall"] == WarnLevel.CRITICAL:
                projected_components.append(
                    {"slot": slot, "component": comp, "wear": projected_wear}
                )

        if today_worst == WarnLevel.CRITICAL:
            continue  # schon heute kritisch -> deckt get_new_component_warnings() ab
        if not projected_components:
            continue

        results.append(
            {
                "bike": bike,
                "predicted_date": predicted_date,
                "predicted_km": predicted_km,
                "components": projected_components,
                "weather": ride_day_weather(bike, predicted_date)["report"],
            }
        )
    return results


# --- Wetter am Fahrtag ------------------------------------------------------------------

#: Unter dieser Schwelle lohnt kein Satz über Wetterverschleiß.
WEATHER_NOTE_MIN_PCT = 5

# Mit Artikel und Verbform, weil der Satz sonst "Bremsen verschleißt" sagt.
_CATEGORY_PHRASES: dict[str, tuple[str, bool]] = {
    ComponentCategory.DRIVETRAIN: ("der Antrieb", False),
    ComponentCategory.BRAKES: ("die Bremsen", True),
    ComponentCategory.WHEELS: ("die Laufräder", True),
    ComponentCategory.SUSPENSION: ("die Federung", False),
    ComponentCategory.COCKPIT: ("das Cockpit", False),
    ComponentCategory.FRAME: ("Rahmen und Lager", True),
    ComponentCategory.ELECTRIC: ("der E-Antrieb", False),
    ComponentCategory.LIGHTING: ("die Beleuchtung", False),
    ComponentCategory.ACCESSORIES: ("das Zubehör", False),
}
_DRIVER_PHRASES = {
    "rain": "Bei Nässe",
    "heat": "Bei der Hitze",
    "cold": "Bei der Kälte",
    "wind": "Bei dem Wind",
}


def weather_wear_note(weather_hourly: dict, categories: Iterable[str]) -> dict:
    """
    Was das vorhergesagte Wetter fürs Rad bedeutet: dieselbe Formel wie für vergangene
    Fahrten (`WeatherWearCalculator`), angewendet auf die Stundenwerte des Fahrfensters.
    Genannt wird die am stärksten betroffene Kategorie unter den montierten Teilen.
    """
    coeffs = {
        c.category: c
        for c in WeatherSensitivityCoefficient.objects.filter(
            category__in=set(categories)
        )
    }
    best: tuple[float, str, str | None] | None = None
    for category, coeff in coeffs.items():
        detail = WeatherWearCalculator.ride_multiplier_detail(weather_hourly, coeff)
        extra = (detail["multiplier"] - 1.0) * 100
        if best is None or extra > best[0]:
            best = (extra, category, detail["dominant_driver"])

    if best is None or best[0] < WEATHER_NOTE_MIN_PCT or best[2] is None:
        return {
            "extra_pct": round(best[0]) if best else 0,
            "category": best[1] if best else None,
            "driver": None,
            "text": "Kein Wetteraufschlag: das Rad verschleißt normal.",
        }

    extra, category, driver = best
    subject, plural = _CATEGORY_PHRASES.get(category, ("das Rad", False))
    verb = "verschleißen" if plural else "verschleißt"
    text = (
        f"{_DRIVER_PHRASES[driver]} {verb} {subject} etwa {round(extra)} % schneller."
    )
    if driver == "rain" and ComponentCategory.DRIVETRAIN in coeffs:
        text += " Die Kette danach reinigen und ölen."
    return {
        "extra_pct": round(extra),
        "category": category,
        "driver": driver,
        "text": text,
    }


def ride_day_weather(bike: Bike, day: date) -> dict:
    """
    Kurzer Wetterbericht für den voraussichtlichen Fahrtag: `{"status": ..., "report": ...}`.

    `status` sagt, warum es keinen Bericht gibt, damit die App nur den Fall erklärt, in
    dem der Nutzer etwas tun kann: `consent_required` (ohne Wetter-Einwilligung gehen keine
    Koordinaten an Open-Meteo), `out_of_range` (weiter als der Vorhersagehorizont),
    `no_location` (keine Fahrt mit Startpunkt), `unavailable` (Open-Meteo-Ausfall).
    Ort ist der Start der letzten Fahrt des Rads — ein Wohnort ist nirgends gespeichert,
    und gefahren wird meist von dort, wo die letzte Fahrt begann.
    """
    from app_auth.consent import has_weather_consent
    from app_dashboard.api.forecast_weather import fetch_day_forecast, in_horizon
    from app_dashboard.models import Ride

    if not has_weather_consent(bike.athlete):
        return {"status": "consent_required", "report": None}
    if not in_horizon(day):
        return {"status": "out_of_range", "report": None}

    start = (
        Ride.objects.filter(bike=bike, start_latlng__isnull=False)
        .order_by("-start_date")
        .values_list("start_latlng", flat=True)
        .first()
    )
    if start is None:
        return {"status": "no_location", "report": None}

    forecast = fetch_day_forecast(start.y, start.x, day)
    if forecast is None:
        return {"status": "unavailable", "report": None}

    categories = (
        ComponentSlot.objects.on_bike(bike)
        .values_list("template__category", flat=True)
        .distinct()
    )
    report = {k: v for k, v in forecast.items() if k != "hourly"}
    report["wear"] = weather_wear_note(forecast["hourly"], categories)
    return {"status": "ok", "report": report}


#: Was die Prognose überhaupt meldet: alles, was am Fahrtag mindestens "bald fällig"
#: ist. Ob das neu ist oder schon heute so steht, entscheidet der Client über
#: `status_today` — dort steht "schon fällig" bereits unter "Als Nächstes dran".
_FORECAST_LEVELS = (WarnLevel.WARN, WarnLevel.CRITICAL)


def next_ride_forecast(bike: Bike) -> dict:
    """
    Die Prognose fürs Dashboard: wann dieses Bike voraussichtlich wieder gefahren
    wird, wie weit, und was bis dahin fällig wird. Dieselbe Projektion wie die
    Vorhersage-Mail (`get_predicted_unsafe_bikes`: Tage-Achse auf den Fahrtag, km- und
    Wetter-Achse um die geschätzte Strecke), aber für **jeden** Abstand und ab "bald
    fällig" statt erst ab kritisch — die Mail ist der Alarm, das hier die Vorschau.

    Liefert die Posten nur als Schlüssel + Status, nicht als eigene Darstellung: der
    Client hat Titel, Baugruppe und Restwert schon aus `assemblies/` und darf sie nicht
    ein zweites Mal anders beschriften.
    """
    from app_dashboard.api.services import (  # inline: Cross-App, siehe Architektur-Notiz
        MIN_RIDES_FOR_PREDICTION,
        ride_rhythm,
    )
    from app_dashboard.models import Ride

    rhythm = ride_rhythm(bike)
    base = {
        "min_rides": MIN_RIDES_FOR_PREDICTION,
        "ride_count": Ride.objects.filter(bike=bike, start_date__isnull=False).count(),
    }
    if rhythm is None:
        return {**base, "predicted_date": None, "items": []}

    predicted_date = rhythm.next_ride
    extra_km = rhythm.median_km or 0.0
    ledger = RideLedger.for_bike(bike.pk)
    items: list[dict] = []

    slots = (
        ComponentSlot.objects.on_bike(bike)
        .select_related("template")
        .prefetch_related("components__periods", "components__checks")
    )
    for slot in slots:
        comp = slot.mounted_component
        if comp is None:
            continue
        then = compute_wear(comp, ledger, as_of=predicted_date, extra_km=extra_km)
        if then["warn_status_overall"] not in _FORECAST_LEVELS:
            continue
        items.append(
            {
                "kind": "part",
                "id": comp.pk,
                "status_today": compute_wear(comp, ledger)["warn_status_overall"],
                "status_then": then["warn_status_overall"],
            }
        )

    intervals = MaintenanceInterval.objects.filter(bike=bike).select_related("assembly")
    for interval in intervals:
        if not interval.is_on_bike:
            continue
        status_then = interval.status(ledger, as_of=predicted_date, extra_km=extra_km)
        if status_then not in _FORECAST_LEVELS:
            continue
        items.append(
            {
                "kind": "interval",
                "id": interval.pk,
                "status_today": interval.status(ledger),
                "status_then": status_then,
            }
        )

    return {
        **base,
        "predicted_date": predicted_date,
        "days_until": (predicted_date - date.today()).days,
        "is_overdue": rhythm.is_overdue,
        "predicted_km": rhythm.median_km,
        "median_gap_days": rhythm.median_gap_days,
        "rides_considered": rhythm.rides_considered,
        "last_ride": rhythm.last_ride,
        "items": items,
        "weather": ride_day_weather(bike, predicted_date),
    }
