"""
Wettervorhersage für den voraussichtlichen Fahrtag (Prognose im Dashboard und
Vorhersage-Mail).

Geholt wird stündlich, aber nur das Fahrfenster (`RIDE_WINDOW_HOURS`, Ortszeit): die
Tiefsttemperatur um 5 Uhr morgens fährt niemand, und ein Gewitter um 23 Uhr auch nicht.
Die Stundenwerte haben **dieselbe Form wie `Ride.weather_data`** (Listen unter
`temperature_2m`/`precipitation`/`wind_speed_10m`), damit
`WeatherWearCalculator.ride_multiplier_detail()` ohne Umweg darauf rechnet — eine Formel
für vergangene und kommende Fahrten.

Datenschutz wie beim Live-Wind im Radcomputer: Koordinaten nur mit Wetter-Einwilligung
(prüft der Aufrufer), auf 2 Nachkommastellen gerundet, nie geloggt.
"""

from __future__ import annotations

import logging
import math
from datetime import date, timedelta

import requests
from django.core.cache import cache

logger = logging.getLogger(__name__)

OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"
#: Open-Meteo rechnet bis 16 Tage voraus (heute + 15).
FORECAST_HORIZON_DAYS = 15
COORD_DIGITS = 2
CACHE_SECONDS = 60 * 60
TIMEOUT_SECONDS = 8
#: Stunden (Ortszeit, inklusive Start, exklusive Ende), in denen typischerweise gefahren wird.
RIDE_WINDOW_HOURS = (8, 20)

HOURLY_FIELDS = (
    "temperature_2m",
    "precipitation",
    "precipitation_probability",
    "wind_speed_10m",
    "wind_gusts_10m",
    "wind_direction_10m",
    "weather_code",
)

# WMO-Wettercodes, gröber zusammengefasst. Höhere Codes sind (grob) das schlimmere
# Wetter, daher bestimmt der höchste Code im Fahrfenster das Wort.
_CONDITIONS: list[tuple[int, str]] = [
    (0, "Klar"),
    (1, "Heiter"),
    (2, "Wolkig"),
    (3, "Bedeckt"),
    (45, "Nebel"),
    (51, "Niesel"),
    (61, "Regen"),
    (66, "Gefrierender Regen"),
    (71, "Schnee"),
    (80, "Schauer"),
    (85, "Schneeschauer"),
    (95, "Gewitter"),
]

_COMPASS = ["Nord", "Nordost", "Ost", "Südost", "Süd", "Südwest", "West", "Nordwest"]


def condition_label(code: int | None) -> str | None:
    if code is None:
        return None
    label = None
    for start, name in _CONDITIONS:
        if code >= start:
            label = name
    return label


def compass_label(direction_deg: float | None) -> str | None:
    """Meteorologisch: die Richtung, aus der der Wind kommt (270 -> 'West')."""
    if direction_deg is None:
        return None
    return _COMPASS[round((direction_deg % 360) / 45) % 8]


def in_horizon(day: date, today: date | None = None) -> bool:
    today = today or date.today()
    return today <= day <= today + timedelta(days=FORECAST_HORIZON_DAYS)


def round_coord(value: float) -> float:
    return round(value, COORD_DIGITS)


def _numbers(values: list) -> list[float]:
    return [float(v) for v in values if isinstance(v, (int, float))]


def _circular_mean(degrees: list[float]) -> float | None:
    if not degrees:
        return None
    x = sum(math.cos(math.radians(d)) for d in degrees)
    y = sum(math.sin(math.radians(d)) for d in degrees)
    if abs(x) < 1e-9 and abs(y) < 1e-9:
        return None
    return math.degrees(math.atan2(y, x)) % 360


def summarize_hours(hourly: dict, day: date) -> dict | None:
    """
    Reduziert die Open-Meteo-Stundenwerte auf das Fahrfenster des Tages. Reine Funktion,
    damit die Tests ohne Netz auskommen.
    """
    times = hourly.get("time") or []
    start, end = RIDE_WINDOW_HOURS
    prefix = day.isoformat()
    picked = [
        i
        for i, stamp in enumerate(times)
        if isinstance(stamp, str)
        and stamp.startswith(prefix)
        and start <= int(stamp[11:13]) < end
    ]
    if not picked:
        return None

    def column(name: str) -> list[float]:
        values = hourly.get(name) or []
        return _numbers([values[i] for i in picked if i < len(values)])

    temps = column("temperature_2m")
    precip = column("precipitation")
    wind = column("wind_speed_10m")
    if not temps or not precip or not wind:
        return None
    gusts = column("wind_gusts_10m")
    probability = column("precipitation_probability")
    codes = column("weather_code")
    code = int(max(codes)) if codes else None
    direction = _circular_mean(column("wind_direction_10m"))

    return {
        "date": day.isoformat(),
        "condition": condition_label(code),
        "weather_code": code,
        "temp_min": round(min(temps), 1),
        "temp_max": round(max(temps), 1),
        "precipitation_mm": round(sum(precip), 1),
        "precipitation_probability": round(max(probability)) if probability else None,
        "wind_kmh": round(sum(wind) / len(wind), 1),
        "gusts_kmh": round(max(gusts), 1) if gusts else None,
        "wind_direction_deg": round(direction) if direction is not None else None,
        "wind_from": compass_label(direction),
        # Eingabe für WeatherWearCalculator, gleiche Form wie Ride.weather_data.
        "hourly": {
            "temperature_2m": temps,
            "precipitation": precip,
            "wind_speed_10m": wind,
        },
    }


def fetch_day_forecast(lat: float, lon: float, day: date) -> dict | None:
    """Vorhersage fürs Fahrfenster an `day`. None bei Ausfall oder außerhalb des Horizonts."""
    if not in_horizon(day):
        return None
    lat, lon = round_coord(lat), round_coord(lon)
    key = f"ride-day-weather:{lat:.{COORD_DIGITS}f}:{lon:.{COORD_DIGITS}f}:{day}"
    cached = cache.get(key)
    if cached is not None:
        return cached

    try:
        res = requests.get(
            OPEN_METEO_FORECAST,
            params={
                "latitude": lat,
                "longitude": lon,
                "hourly": ",".join(HOURLY_FIELDS),
                "wind_speed_unit": "kmh",
                "timezone": "auto",
                "start_date": day.isoformat(),
                "end_date": day.isoformat(),
            },
            timeout=TIMEOUT_SECONDS,
        )
        res.raise_for_status()
        hourly = res.json().get("hourly") or {}
    except (requests.RequestException, ValueError, AttributeError) as exc:
        # Keine Koordinaten im Log, nur die Art des Fehlers.
        logger.warning(
            "Open-Meteo (Fahrtag-Vorhersage) fehlgeschlagen: %s", type(exc).__name__
        )
        return None

    summary = summarize_hours(hourly, day)
    if summary is not None:
        cache.set(key, summary, CACHE_SECONDS)
    return summary
