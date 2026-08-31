"""
Gegenwind-Berechnung — die einzige Quelle fuer alles Windbezogene.

Vorher existierten drei voneinander unabhaengige Berechnungen mit zwangslaeufig
verschiedenen Ergebnissen: die Kopfzeilen-Zahl (Luftlinie Start->Ziel der ganzen
Fahrt), das Chart (+-300s-Fenster um jede volle Stunde) und die Karte (Interpolation
ueber den Segment-Index des RDP-vereinfachten Tracks). Letzteres war grundsaetzlich
falsch: RDP behaelt in Kurven viele und auf Geraden wenige Punkte, Index-Fortschritt
entspricht also nicht dem Zeit-Fortschritt.

Hier wird stattdessen die Route in Abschnitte *gleicher Distanz* geteilt und je
Abschnitt der tatsaechliche Kurs gegen den zu diesem Zeitpunkt interpolierten Wind
gerechnet. Kurven bekommen dadurch automatisch einen eigenen Gegenwind-Wert, und
`average_headwind()` ist per Konstruktion das Mittel dessen, was die Karte zeigt.

Die Formeln `calculate_heading`/`calculate_headwind` stammen unveraendert aus dem
frueheren `utils.py` — korrekt waren sie immer, nur ihre Eingaben nicht. Sie liegen
jetzt hier, weil `utils.py` danach nur noch aus toten bzw. weitergereichten
Funktionen bestanden haette.
"""

import math
from datetime import datetime, timedelta, timezone

import dateutil.parser

EARTH_RADIUS_KM = 6371.0

# Zielanzahl Abschnitte pro Fahrt. 200 ergibt bei einer 50-km-Runde ~250-m-Abschnitte —
# fein genug, dass einzelne Kurven eigene Farben bekommen, grob genug fuer die Payload.
DEFAULT_TARGET_SEGMENTS = 200

# Obergrenze fuer die Stuetzpunkte, die aus dem Roh-Stream in die Segment-Geometrie
# uebernommen werden. Strava liefert teils >10.000 Punkte; ohne Deckel waechst die
# GeoJSON-Antwort unnoetig, ohne dass man den Unterschied auf der Karte saehe.
MAX_GEOMETRY_POINTS = 1500

WIND_SOURCE_STREAM = "stream"
WIND_SOURCE_COARSE = "coarse"


def calculate_headwind(ride_heading, wind_direction, wind_speed):
    """
    Berechnet den Gegenwindanteil in km/h.

    Positiver Wert = Gegenwind, negativer Wert = Rueckenwind.
    """
    theta = math.radians(wind_direction - ride_heading)
    return round(wind_speed * math.cos(theta), 1)


def calculate_heading(lat1, lon1, lat2, lon2):
    """
    Berechnet den Kurswinkel in Grad (0 = Nord, 90 = Ost, 180 = Sued, 270 = West).
    """
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_lambda = math.radians(lon2 - lon1)

    y = math.sin(delta_lambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(
        delta_lambda
    )

    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _to_aware(value):
    """ISO-String oder datetime -> timezone-aware datetime (UTC als Default)."""
    if value is None:
        return None
    dt = dateutil.parser.isoparse(value) if isinstance(value, str) else value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Grosskreis-Distanz zwischen zwei WGS84-Punkten in km."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def _round_or_none(value, digits: int = 1):
    return None if value is None else round(value, digits)


def _hour_epochs(hourly: dict) -> list[float]:
    """Stunden-Zeitstempel der Open-Meteo-Antwort als Unix-Sekunden."""
    return [_to_aware(t).timestamp() for t in (hourly.get("time") or [])]


def _bracket(epochs: list[float], target: float) -> tuple[int, int, float]:
    """
    Indizes der beiden Stundenwerte, die `target` einschliessen, plus den
    Interpolationsanteil t in [0, 1]. Ausserhalb der Reihe wird geklemmt.
    """
    if len(epochs) == 1:
        return 0, 0, 0.0
    if target <= epochs[0]:
        return 0, 0, 0.0
    if target >= epochs[-1]:
        last = len(epochs) - 1
        return last, last, 0.0

    hi = 1
    while hi < len(epochs) and epochs[hi] < target:
        hi += 1
    lo = hi - 1
    span = epochs[hi] - epochs[lo]
    t = (target - epochs[lo]) / span if span else 0.0
    return lo, hi, t


def _interpolate(epochs, values, target) -> float | None:
    """
    Linear zwischen den zwei umschliessenden Stundenwerten. Nearest-Hour waere
    einfacher, laesst die Farbe an Stundengrenzen aber sichtbar springen.
    """
    if not epochs or not values:
        return None
    lo, hi, t = _bracket(epochs, target)
    if lo >= len(values) or hi >= len(values):
        return None
    a, b = values[lo], values[hi]
    if a is None or b is None:
        return a if b is None else b
    return a + (b - a) * t


def _interpolate_direction(epochs, directions, target) -> float | None:
    """
    Wie `_interpolate`, aber zirkulaer: zwischen 350 Grad und 10 Grad liegt 0 Grad,
    nicht 180 Grad. Linear interpoliert waere die Windrichtung dort exakt
    entgegengesetzt — genau in der Situation, in der sich der Wind kaum dreht.
    """
    if not epochs or not directions:
        return None
    lo, hi, t = _bracket(epochs, target)
    if lo >= len(directions) or hi >= len(directions):
        return None
    a, b = directions[lo], directions[hi]
    if a is None or b is None:
        return a if b is None else b

    delta = ((b - a + 180) % 360) - 180
    return (a + delta * t) % 360


def _subsample(latlngs: list, times: list, max_points: int) -> tuple[list, list]:
    """Gleichmaessiges Ausduennen unter `max_points`, Start und Ziel bleiben erhalten."""
    if len(latlngs) <= max_points:
        return latlngs, times
    step = math.ceil(len(latlngs) / max_points)
    thinned_points = latlngs[::step]
    thinned_times = times[::step]
    if thinned_points[-1] != latlngs[-1]:
        thinned_points.append(latlngs[-1])
        thinned_times.append(times[-1])
    return thinned_points, thinned_times


def _segment_values(hour_epochs: list[float], hourly: dict, mid_epoch: float) -> dict:
    """Wetterwerte eines Abschnitts, auf dessen Mittel-Zeitpunkt interpoliert."""
    return {
        "wind_direction": _interpolate_direction(
            hour_epochs, hourly.get("wind_direction_10m"), mid_epoch
        ),
        "wind_speed": _interpolate(
            hour_epochs, hourly.get("wind_speed_10m"), mid_epoch
        ),
        "precipitation": _interpolate(
            hour_epochs, hourly.get("precipitation"), mid_epoch
        ),
        "temperature": _interpolate(
            hour_epochs, hourly.get("temperature_2m"), mid_epoch
        ),
    }


def build_wind_segments(
    latlngs: list,
    times: list,
    start_time,
    hourly: dict,
    target_segments: int = DEFAULT_TARGET_SEGMENTS,
) -> list[dict]:
    """
    Zerlegt eine Fahrt in Abschnitte gleicher Distanz und berechnet je Abschnitt den
    Gegenwind aus dem *dortigen* Kurs und dem zu diesem Zeitpunkt herrschenden Wind.

    :param latlngs: Roh-GPS-Stream als [[lat, lon], ...] (Strava-Reihenfolge)
    :param times:   Sekunden seit Fahrtstart, gleiche Laenge wie `latlngs`
    :param start_time: Startzeitpunkt der Fahrt (datetime oder ISO-String)
    :param hourly:  Open-Meteo-`hourly`-Block (time/wind_direction_10m/wind_speed_10m/...)

    Gibt eine leere Liste zurueck, wenn die Eingaben fuer eine sinnvolle Aussage nicht
    reichen — die Aufrufer fallen dann auf `build_coarse_wind_segment()` zurueck.
    """
    start = _to_aware(start_time)
    if start is None or not latlngs or not times or len(latlngs) != len(times):
        return []
    if len(latlngs) < 2 or not hourly or not hourly.get("time"):
        return []

    points, point_times = _subsample(list(latlngs), list(times), MAX_GEOMETRY_POINTS)

    # Kumulative Distanz je Stuetzpunkt — Basis fuer die distanzgleiche Aufteilung.
    cumulative = [0.0]
    for i in range(1, len(points)):
        cumulative.append(
            cumulative[-1]
            + haversine_km(
                points[i - 1][0], points[i - 1][1], points[i][0], points[i][1]
            )
        )

    total_km = cumulative[-1]
    if total_km <= 0:
        return []

    segment_count = max(1, min(target_segments, len(points) - 1))
    segment_km = total_km / segment_count

    start_epoch = start.timestamp()
    hour_epochs = _hour_epochs(hourly)

    segments: list[dict] = []
    chunk_start = 0

    for n in range(1, segment_count + 1):
        boundary_km = segment_km * n
        chunk_end = chunk_start
        while chunk_end < len(points) - 1 and cumulative[chunk_end] < boundary_km:
            chunk_end += 1
        if n == segment_count:
            chunk_end = len(points) - 1
        if chunk_end <= chunk_start:
            continue

        coords = [[p[1], p[0]] for p in points[chunk_start : chunk_end + 1]]
        lat1, lon1 = points[chunk_start][0], points[chunk_start][1]
        lat2, lon2 = points[chunk_end][0], points[chunk_end][1]

        bearing = (
            None
            if (lat1, lon1) == (lat2, lon2)
            else calculate_heading(lat1, lon1, lat2, lon2)
        )
        t_start = point_times[chunk_start]
        t_end = point_times[chunk_end]
        values = _segment_values(
            hour_epochs, hourly, start_epoch + (t_start + t_end) / 2
        )

        headwind = None
        if (
            bearing is not None
            and values["wind_direction"] is not None
            and values["wind_speed"] is not None
        ):
            headwind = calculate_headwind(
                bearing, values["wind_direction"], values["wind_speed"]
            )

        segments.append(
            {
                "coords": coords,
                "bearing": round(bearing, 1) if bearing is not None else None,
                "headwind": headwind,
                "wind_speed": _round_or_none(values["wind_speed"]),
                "wind_direction": _round_or_none(values["wind_direction"]),
                "precipitation": _round_or_none(values["precipitation"], 2),
                "temperature": _round_or_none(values["temperature"]),
                "distance_km": round(
                    cumulative[chunk_end] - cumulative[chunk_start], 3
                ),
                "t_start": t_start,
                "t_end": t_end,
            }
        )
        chunk_start = chunk_end

    return segments


def build_coarse_wind_segment(
    track_coords: list, start_time, elapsed_time, hourly: dict
) -> list[dict]:
    """
    Notfall-Variante ohne GPS-Stream (Stream-Abruf war fehlgeschlagen): ein einziger
    Abschnitt aus dem vereinfachten `Ride.track` mit Start-Ziel-Kurs. Bewusst grob —
    die Aufrufer markieren das Ergebnis mit `wind_source="coarse"`, damit die UI eine
    Schaetzung ausweisen kann statt falsche Praezision vorzutaeuschen.

    :param track_coords: [(lon, lat), ...] wie von `LineString.coords`
    """
    start = _to_aware(start_time)
    if start is None or not track_coords or len(track_coords) < 2:
        return []
    if not hourly or not hourly.get("time"):
        return []

    lon1, lat1 = track_coords[0][0], track_coords[0][1]
    lon2, lat2 = track_coords[-1][0], track_coords[-1][1]
    if (lat1, lon1) == (lat2, lon2):
        return []

    bearing = calculate_heading(lat1, lon1, lat2, lon2)
    duration = elapsed_time or 0
    values = _segment_values(
        _hour_epochs(hourly), hourly, start.timestamp() + duration / 2
    )

    if values["wind_direction"] is None or values["wind_speed"] is None:
        return []

    return [
        {
            "coords": [[c[0], c[1]] for c in track_coords],
            "bearing": round(bearing, 1),
            "headwind": calculate_headwind(
                bearing, values["wind_direction"], values["wind_speed"]
            ),
            "wind_speed": _round_or_none(values["wind_speed"]),
            "wind_direction": _round_or_none(values["wind_direction"]),
            "precipitation": _round_or_none(values["precipitation"], 2),
            "temperature": _round_or_none(values["temperature"]),
            "distance_km": round(haversine_km(lat1, lon1, lat2, lon2), 3),
            "t_start": 0,
            "t_end": duration,
        }
    ]


def average_headwind(segments: list[dict]) -> float | None:
    """
    Distanzgewichtetes Mittel des Gegenwinds ueber alle Abschnitte — die Zahl in der
    Kopfzeile der Aktivitaet. Distanz- statt Zeitgewichtung, damit sie zu dem passt,
    was die Karte flaechig zeigt; langsame Bergabschnitte bekommen sonst ein Gewicht,
    das ihrer Sichtbarkeit auf der Karte widerspricht.
    """
    weighted = 0.0
    total_km = 0.0
    for segment in segments:
        if segment.get("headwind") is None:
            continue
        km = segment.get("distance_km") or 0.0
        if km <= 0:
            continue
        weighted += segment["headwind"] * km
        total_km += km
    return round(weighted / total_km, 1) if total_km > 0 else None


def hourly_headwind(segments: list[dict], hourly: dict, start_time) -> list[float]:
    """
    Gegenwind je Wetterstunde fuer das Chart — aus dem Kurs des zeitlich naechsten
    Abschnitts und dem Wind *dieser* Stunde. Dadurch nutzt das Chart dieselben Kurse
    wie die Karte und dieselben Stundenwerte wie die daneben gezeichnete Windkurve.
    """
    times = hourly.get("time") or []
    directions = hourly.get("wind_direction_10m") or []
    speeds = hourly.get("wind_speed_10m") or []
    start = _to_aware(start_time)
    if not times or not segments or start is None:
        return [0.0] * len(times)

    start_epoch = start.timestamp()
    results: list[float] = []

    for i, time_str in enumerate(times):
        offset = _to_aware(time_str).timestamp() - start_epoch
        nearest = min(
            segments, key=lambda s: abs((s["t_start"] + s["t_end"]) / 2 - offset)
        )
        bearing = nearest.get("bearing")
        w_dir = directions[i] if i < len(directions) else None
        w_speed = speeds[i] if i < len(speeds) else None
        if bearing is None or w_dir is None or w_speed is None:
            results.append(0.0)
        else:
            results.append(calculate_headwind(bearing, w_dir, w_speed))

    return results


def compute_ride_wind(
    ride, hourly: dict, stream_data: dict | None = None
) -> tuple[list[dict], str]:
    """
    Windabschnitte einer Fahrt — der gemeinsame Einstieg fuer Import und Detail-View,
    damit beide garantiert dieselben Zahlen sehen.

    :param hourly: Open-Meteo-`hourly`-Block. Beim Import frisch abgerufen, im
        Detail-Request aus `ride.weather_data` rekonstruiert (gleiche Feldnamen).
    :param stream_data: Roh-Antwort von `StravaStreamService.fetch_activity_streams()`.
        Ohne Angabe wird der gespeicherte `ride.streams` genutzt.

    Gibt `(segments, wind_source)` zurueck; `wind_source` ist `"stream"` bei echter
    Abschnittsberechnung und `"coarse"` beim Start-Ziel-Fallback ohne GPS-Stream.
    """
    if not hourly or not hourly.get("time"):
        return [], WIND_SOURCE_COARSE

    if stream_data is not None:
        latlngs = (stream_data.get("latlng") or {}).get("data") or []
        times = (stream_data.get("time") or {}).get("data") or []
    else:
        stream = getattr(ride, "streams", None)
        latlngs = getattr(stream, "latlngs", None) or []
        times = getattr(stream, "time_series", None) or []

    segments = build_wind_segments(latlngs, times, ride.start_date, hourly)
    if segments:
        return segments, WIND_SOURCE_STREAM

    track_coords = list(ride.track.coords) if ride.track else []
    return (
        build_coarse_wind_segment(
            track_coords, ride.start_date, ride.elapsed_time, hourly
        ),
        WIND_SOURCE_COARSE,
    )


def hourly_from_weather_data(weather_data: dict | None) -> dict:
    """
    Rekonstruiert einen `hourly`-Block aus dem gespeicherten `Ride.weather_data`, damit
    der Detail-Request die Abschnitte ohne erneuten Open-Meteo-Aufruf berechnen kann.
    Setzt voraus, dass `wind_direction_10m` mitgespeichert wurde (siehe
    `utils.get_filtered_weather`) — bei Altdaten ohne dieses Feld bleibt nur der
    grobe Fallback, bis `recompute_wind` durchgelaufen ist.
    """
    weather_data = weather_data or {}
    if not weather_data.get("time") or not weather_data.get("wind_direction_10m"):
        return {}
    return {
        "time": weather_data.get("time") or [],
        "wind_direction_10m": weather_data.get("wind_direction_10m") or [],
        "wind_speed_10m": weather_data.get("wind_speed_10m") or [],
        "precipitation": weather_data.get("precipitation") or [],
        "temperature_2m": weather_data.get("temperature_2m") or [],
    }


def get_filtered_weather(ride, weather_data=None, segments=None) -> dict:
    """
    Schneidet die Open-Meteo-Stundenwerte auf den Zeitraum der Fahrt zu — das, was als
    `Ride.weather_data` persistiert wird.

    Der `headwind`-Zeitverlauf wird nicht mehr eigenstaendig berechnet, sondern aus den
    Windabschnitten abgeleitet (`hourly_headwind`); sonst haetten Chart und Karte
    weiterhin unterschiedliche Kurse und damit unterschiedliche Zahlen.
    `wind_direction_10m` wird mitgespeichert, damit der Detail-Request die Abschnitte
    ohne erneuten Open-Meteo-Aufruf rekonstruieren kann.
    """
    if not weather_data or "hourly" not in weather_data:
        return {}

    hourly = weather_data["hourly"]
    start_time = _to_aware(ride.start_date)
    if start_time is None:
        return {}

    end_time = start_time + timedelta(seconds=ride.elapsed_time or 0)
    start_hour = start_time.replace(minute=0, second=0, microsecond=0)
    end_hour = end_time.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    filtered_indices = []
    for i, time_str in enumerate(hourly.get("time") or []):
        weather_time = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
        if weather_time.tzinfo is None:
            weather_time = weather_time.replace(tzinfo=timezone.utc)
        if start_hour <= weather_time <= end_hour:
            filtered_indices.append(i)

    def _column(key):
        values = hourly.get(key) or []
        return [values[i] for i in filtered_indices if i < len(values)]

    filtered = {
        "time": _column("time"),
        "temperature_2m": _column("temperature_2m"),
        "wind_speed_10m": _column("wind_speed_10m"),
        "wind_direction_10m": _column("wind_direction_10m"),
        "precipitation": _column("precipitation"),
    }
    filtered["headwind"] = hourly_headwind(segments or [], filtered, start_time)
    return filtered


def segments_to_geojson(segments: list[dict], wind_source: str) -> dict:
    """GeoJSON-FeatureCollection fuer die Karte — ein Feature je Abschnitt."""
    return {
        "type": "FeatureCollection",
        "wind_source": wind_source,
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "index": i,
                    "headwind": s["headwind"],
                    "wind_speed": s["wind_speed"],
                    "wind_direction": s["wind_direction"],
                    "bearing": s["bearing"],
                    "precipitation": s["precipitation"],
                    "temperature": s["temperature"],
                    "distance_km": s["distance_km"],
                },
                "geometry": {"type": "LineString", "coordinates": s["coords"]},
            }
            for i, s in enumerate(segments)
        ],
    }
