import bisect
import math
import dateutil.parser
from datetime import datetime, timedelta, timezone


def find_hourly_index(times, start_date):
    """
    Gibt den Index in der Stundenliste zurück, der am nächsten an start_date liegt.
    Fällt auf Index 0 zurück, wenn kein passender Eintrag gefunden wird.
    """
    if not times or not start_date:
        return 0

    if isinstance(start_date, str):
        start_time = dateutil.parser.isoparse(start_date)
    else:
        start_time = start_date

    if start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=timezone.utc)

    target_hour = start_time.replace(minute=0, second=0, microsecond=0)

    for i, time_str in enumerate(times):
        t = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        if t == target_hour:
            return i

    return 0


def calculate_headwind(ride_heading, wind_direction, wind_speed):
    """
    Berechnet den Gegenwindanteil in km/h.

    Positiver Wert = Gegenwind, 0 = Rückenwind oder kein Wind.
    """
    theta = math.radians(wind_direction - ride_heading)
    return round(wind_speed * math.cos(theta), 1)


def calculate_heading(lat1, lon1, lat2, lon2):
    """
    Berechnet den Kurswinkel in Grad (0 = Nord, 90 = Ost, 180 = Süd, 270 = West).
    """
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_lambda = math.radians(lon2 - lon1)

    y = math.sin(delta_lambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(
        delta_lambda
    )

    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _local_heading(latlngs, times, target_seconds, window_seconds=300):
    """
    Kurswinkel zu einem bestimmten Zeitpunkt der Fahrt, berechnet aus den
    GPS-Punkten in einem Zeitfenster um target_seconds (statt der groben
    Start-Ziel-Gerade der gesamten Fahrt).
    """
    if len(latlngs) < 2 or len(times) != len(latlngs):
        return None

    start_idx = bisect.bisect_left(times, target_seconds - window_seconds)
    end_idx = bisect.bisect_right(times, target_seconds + window_seconds) - 1
    start_idx = max(0, start_idx)
    end_idx = min(len(latlngs) - 1, end_idx)

    if end_idx <= start_idx:
        return None

    lat1, lon1 = latlngs[start_idx]
    lat2, lon2 = latlngs[end_idx]
    if (lat1, lon1) == (lat2, lon2):
        return None

    return calculate_heading(lat1, lon1, lat2, lon2)


def get_filtered_weather(ride, weather_data=None, stream_data=None):
    """Filtert die Wetterdaten und berechnet den Gegenwind."""
    if not weather_data or "hourly" not in weather_data:
        return {}

    hourly = weather_data["hourly"]

    # 1. Startzeiten und Zeitraum definieren
    if isinstance(ride.start_date, str):
        start_time = dateutil.parser.isoparse(ride.start_date)
    else:
        start_time = ride.start_date

    if start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=timezone.utc)

    duration_seconds = ride.elapsed_time or 0
    end_time = start_time + timedelta(seconds=duration_seconds)

    start_hour = start_time.replace(minute=0, second=0, microsecond=0)
    end_hour = end_time.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    # 2. Fallback-Heading aus Start-/Endpunkt der ganzen Fahrt (falls keine
    # GPS-Stream-Daten mit Zeitstempeln vorliegen).
    fallback_heading = None
    if ride.start_latlng and ride.track:
        lat1, lon1 = ride.start_latlng.coords[1], ride.start_latlng.coords[0]
        lat2, lon2 = ride.track.coords[-1][1], ride.track.coords[-1][0]
        fallback_heading = calculate_heading(lat1, lon1, lat2, lon2)

    stream_latlngs = (stream_data or {}).get("latlng", {}).get("data") or []
    stream_times = (stream_data or {}).get("time", {}).get("data") or []
    has_stream = len(stream_latlngs) >= 2 and len(stream_times) == len(stream_latlngs)

    # 3. Alles in EINER Schleife verarbeiten
    filtered_indices = []
    headwind_results = []

    for i, time_str in enumerate(hourly["time"]):
        weather_time = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
        if weather_time.tzinfo is None:
            weather_time = weather_time.replace(tzinfo=timezone.utc)

        if start_hour <= weather_time <= end_hour:
            filtered_indices.append(i)

            w_dir = hourly["wind_direction_10m"][i]
            w_speed = hourly["wind_speed_10m"][i]

            heading = fallback_heading
            if has_stream:
                elapsed_seconds = (weather_time - start_time).total_seconds()
                local_heading = _local_heading(stream_latlngs, stream_times, elapsed_seconds)
                if local_heading is not None:
                    heading = local_heading

            headwind_results.append(
                calculate_headwind(heading, w_dir, w_speed) if heading is not None else 0.0
            )

    return {
        "time": [hourly["time"][i] for i in filtered_indices],
        "temperature_2m": [hourly["temperature_2m"][i] for i in filtered_indices],
        "wind_speed_10m": [hourly["wind_speed_10m"][i] for i in filtered_indices],
        "precipitation": [hourly["precipitation"][i] for i in filtered_indices],
        "headwind": headwind_results,
    }
