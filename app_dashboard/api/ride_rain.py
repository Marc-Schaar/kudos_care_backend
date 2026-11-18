"""
Regen während einer Fahrt: die einzige Quelle für „wie nass war diese Fahrt".

Bevorzugt werden die 15-Minuten-Werte, die beim Import unter
`Ride.weather_data["ride_rain"]` landen. Fehlen sie (Abruf fehlgeschlagen, Altfahrt),
zählen die Stundenwerte, gewichtet mit dem Anteil, den die Fahrt an jeder Stunde hat. Das
gespeicherte Stundenfenster reicht über Start und Ziel hinaus; ein einfacher Mittelwert
zählte Stunden mit, in denen gar nicht gefahren wurde.

Abgefragt wird der Startpunkt der Fahrt, das deckt die Wetter-Einwilligung ab. Punkte entlang
der Strecke gehen nur mit der eigenen Einwilligung `route_weather_consent_at` an Open-Meteo,
je Viertelstunde einer, auf rund 1 km gerundet. Gespeichert werden nur die Regenwerte.

Open-Meteo meldet zur Zeit T die Menge des Zeitraums davor: bei Stundenwerten die letzte
Stunde, bei `minutely_15` die letzten 15 Minuten, jeweils in mm.
"""

import math
from bisect import bisect_left
from datetime import datetime, timedelta, timezone

import dateutil.parser

QUARTER_HOUR = timedelta(minutes=15)
HOUR = timedelta(hours=1)
QUARTERS_PER_HOUR = 4

SOURCE_START_POINT = "minutely_15"
SOURCE_ROUTE = "route_minutely_15"

# Zwei Nachkommastellen sind rund 1 km: genauer löst das Wettermodell ohnehin nicht auf.
ROUTE_COORD_DECIMALS = 2
# Deckel für lange Fahrten, damit eine einzelne Abfrage überschaubar bleibt.
MAX_ROUTE_LOCATIONS = 12


def to_aware(value) -> datetime | None:
    """ISO-String oder datetime -> timezone-aware datetime (UTC als Default)."""
    if value is None:
        return None
    dt = dateutil.parser.isoparse(value) if isinstance(value, str) else value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _overlap_seconds(
    slot_start: datetime, slot_end: datetime, start: datetime, end: datetime
) -> float:
    return (min(slot_end, end) - max(slot_start, start)).total_seconds()


def route_rain_locations(
    start_date, elapsed_time: int | None, stream_data: dict | None
) -> list[tuple[str, float, float]]:
    """
    Wo die Fahrt in jeder Viertelstunde war: `(Zeitstempel, lat, lon)` je Viertelstunde,
    gerundet. Lange Fahrten teilen sich Orte, damit es höchstens `MAX_ROUTE_LOCATIONS` sind.
    Leer ohne brauchbaren GPS-Stream.
    """
    start = to_aware(start_date)
    latlngs = ((stream_data or {}).get("latlng") or {}).get("data") or []
    times = ((stream_data or {}).get("time") or {}).get("data") or []
    if start is None or not elapsed_time or not latlngs or len(latlngs) != len(times):
        return []

    end = start + timedelta(seconds=elapsed_time)
    floored = start.replace(
        minute=start.minute - start.minute % 15, second=0, microsecond=0
    )
    slot_ends = [floored + QUARTER_HOUR]
    while slot_ends[-1] < end:
        slot_ends.append(slot_ends[-1] + QUARTER_HOUR)

    chunk_size = math.ceil(len(slot_ends) / MAX_ROUTE_LOCATIONS)
    locations: list[tuple[str, float, float]] = []
    for first in range(0, len(slot_ends), chunk_size):
        chunk = slot_ends[first : first + chunk_size]
        ridden_from = max(chunk[0] - QUARTER_HOUR, start)
        ridden_to = min(chunk[-1], end)
        middle = (ridden_from + (ridden_to - ridden_from) / 2 - start).total_seconds()
        index = min(bisect_left(times, middle), len(latlngs) - 1)
        lat = round(latlngs[index][0], ROUTE_COORD_DECIMALS)
        lon = round(latlngs[index][1], ROUTE_COORD_DECIMALS)
        locations.extend(
            (slot_end.strftime("%Y-%m-%dT%H:%M"), lat, lon) for slot_end in chunk
        )
    return locations


def merge_route_minutely(
    locations: list[tuple[str, float, float]],
    forecasts: dict[tuple[float, float], dict],
) -> dict:
    """Setzt je Viertelstunde den Wert des Ortes ein, an dem die Fahrt gerade war."""
    lookups = {
        place: dict(zip(data.get("time") or [], data.get("precipitation") or []))
        for place, data in forecasts.items()
    }
    return {
        "time": [label for label, _, _ in locations],
        "precipitation": [
            lookups.get((lat, lon), {}).get(label) for label, lat, lon in locations
        ],
    }


def build_ride_rain(
    start_date,
    elapsed_time: int | None,
    minutely: dict | None,
    source: str = SOURCE_START_POINT,
):
    """
    Verdichtet den `minutely_15`-Block auf die Viertelstunden der Fahrt.

    Gibt `{"source", "mm_h", "time", "precipitation"}` zurück, `precipitation` in mm/h.
    `None`, wenn die Fahrt nicht lückenlos abgedeckt ist: Ein fehlender Wert ist unbekannt,
    nicht trocken, und dann gelten weiter die Stundenwerte.
    """
    start = to_aware(start_date)
    times = (minutely or {}).get("time") or []
    values = (minutely or {}).get("precipitation") or []
    if start is None or not elapsed_time or not times or len(times) != len(values):
        return None

    end = start + timedelta(seconds=elapsed_time)
    labels: list[str] = []
    rates: list[float] = []
    weighted = 0.0
    covered = 0.0

    for label, value in zip(times, values):
        slot_end = to_aware(label)
        overlap = _overlap_seconds(slot_end - QUARTER_HOUR, slot_end, start, end)
        if overlap <= 0:
            continue
        if value is None:
            return None
        rate = value * QUARTERS_PER_HOUR
        labels.append(label)
        rates.append(round(rate, 2))
        weighted += rate * overlap
        covered += overlap

    if covered < elapsed_time:
        return None
    return {
        "source": source,
        "mm_h": round(weighted / covered, 3),
        "time": labels,
        "precipitation": rates,
    }


def ride_precipitation(
    weather_data: dict | None, start_date=None, elapsed_time: int | None = None
) -> float | None:
    """
    Niederschlag während der Fahrt in mm/h. Ohne Fahrtzeiten bleibt es beim Mittelwert,
    etwa für die Vorhersage eines Fahrtages, zu der es noch keine Fahrt gibt.
    """
    data = weather_data or {}
    stored = (data.get("ride_rain") or {}).get("mm_h")
    if stored is not None:
        return stored

    values = data.get("precipitation") or []
    known = [value for value in values if value is not None]
    if not known:
        return None
    mean = sum(known) / len(known)

    times = data.get("time") or []
    start = to_aware(start_date)
    if start is None or not elapsed_time or len(times) != len(values):
        return mean

    end = start + timedelta(seconds=elapsed_time)
    weighted = 0.0
    covered = 0.0
    for label, value in zip(times, values):
        if value is None:
            continue
        hour_end = to_aware(label)
        overlap = _overlap_seconds(hour_end - HOUR, hour_end, start, end)
        if overlap > 0:
            weighted += value * overlap
            covered += overlap
    return weighted / covered if covered else mean


def apply_ride_rain(segments: list[dict], start_date, ride_rain: dict | None) -> None:
    """
    Überschreibt den Regen je Abschnitt mit den 15-Minuten-Werten, damit Karte und
    Fahrtenliste dieselbe Quelle haben. Abschnitte ohne Überlappung behalten ihren Wert.
    """
    start = to_aware(start_date)
    if start is None or not ride_rain:
        return

    slots = [
        (to_aware(label), rate)
        for label, rate in zip(
            ride_rain.get("time") or [], ride_rain.get("precipitation") or []
        )
    ]
    for segment in segments:
        segment_start = start + timedelta(seconds=segment["t_start"])
        segment_end = start + timedelta(seconds=segment["t_end"])
        weighted = 0.0
        covered = 0.0
        for slot_end, rate in slots:
            overlap = _overlap_seconds(
                slot_end - QUARTER_HOUR, slot_end, segment_start, segment_end
            )
            if overlap > 0:
                weighted += rate * overlap
                covered += overlap
        if covered:
            segment["precipitation"] = round(weighted / covered, 2)
