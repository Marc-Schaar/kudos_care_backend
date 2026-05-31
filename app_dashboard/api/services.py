import logging
import math
from datetime import datetime, timedelta, timezone

import dateutil.parser
import polyline
import requests
from shapely.geometry import LineString as ShapelyLineString

from django.contrib.gis.geos import LineString as DjangoLineString, Point

from ..models import Ride, RideStream
from .utils import (
    find_hourly_index,
    calculate_headwind,
    calculate_heading,
    get_filtered_weather,
)

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 10


class StravaImportService:
    @staticmethod
    def sync_activity_to_db(activity_data, profile):
        """
        Wandelt Strava-JSON in ein Ride-Objekt um und speichert es in PostGIS.
        """
        access_token = profile.access_token
        polyline_str = activity_data.get("map", {}).get("summary_polyline")
        start_date = activity_data.get("start_date_local", "").split("T")[0]
        start_latlng = activity_data.get("start_latlng")

        track = None
        weather_info = None
        point = None

        if polyline_str:
            coords = [(lon, lat) for lat, lon in polyline.decode(polyline_str)]
            reduced_coords = GeoSimplifyService.reduce_track(coords, tolerance=0.001)
            track = DjangoLineString(reduced_coords, srid=4326)

        if start_latlng and len(start_latlng) == 2:
            point = Point(start_latlng[1], start_latlng[0], srid=4326)

        ride, created = Ride.objects.update_or_create(
            strava_id=activity_data["id"],
            defaults={
                "name": activity_data.get("name"),
                "track": track,
                "start_latlng": point,
                "distance": activity_data.get("distance"),
                "start_date": activity_data.get("start_date_local"),
                "elapsed_time": activity_data.get("elapsed_time"),
                "athlete": profile.id,
            },
        )

        try:
            stream_data = StravaStreamService.fetch_activity_streams(
                ride.strava_id, access_token
            )
        except requests.exceptions.RequestException as e:
            logger.error(
                "Stream-Abruf fehlgeschlagen für Ride %s: %s", ride.strava_id, e
            )
            stream_data = None

        if start_latlng and start_date:
            weather_info = WeatherService.get_historical_weather(
                start_latlng[0], start_latlng[1], start_date
            )
            avg_headwind = (
                WeatherService.analyze_wind(stream_data, weather_info, ride)
                if stream_data and weather_info
                else 0.0
            )
            ride.weather_data = {
                **get_filtered_weather(ride, weather_info),
                "avg_headwind": avg_headwind,
            }
            ride.save()

        if stream_data:
            RideStream.objects.update_or_create(
                ride=ride,
                defaults={
                    "latlngs": stream_data.get("latlng", {}).get("data"),
                    "time_series": stream_data.get("time", {}).get("data"),
                    "avg_headwind": (ride.weather_data or {}).get("avg_headwind", 0.0),
                },
            )

        return ride


class StravaStreamService:
    @staticmethod
    def fetch_activity_streams(activity_id, access_token):
        url = f"https://www.strava.com/api/v3/activities/{activity_id}/streams"
        params = {"keys": "latlng,time", "key_by_type": "true"}
        headers = {"Authorization": f"Bearer {access_token}"}

        response = requests.get(
            url, headers=headers, params=params, timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        return response.json()


class GeoSimplifyService:
    @staticmethod
    def reduce_track(points, tolerance=0.001):
        """
        Vereinfacht einen Track mit dem Ramer-Douglas-Peucker-Algorithmus.

        :param points: Liste von (lon, lat) Tupeln
        :param tolerance: Grad der Vereinfachung (0.001 ≈ 100–110 m)
        """
        line = ShapelyLineString(points)
        simplified = line.simplify(tolerance, preserve_topology=True)
        return list(simplified.coords)


class WeatherService:
    @staticmethod
    def get_historical_weather(lat, lon, date):
        """Ruft das historische Wetter für einen Punkt an einem Datum ab."""
        url = "https://archive-api.open-meteo.com/v1/archive"
        params = {
            "latitude": lat,
            "longitude": lon,
            "start_date": date,
            "end_date": date,
            "hourly": "temperature_2m,precipitation,wind_speed_10m,wind_direction_10m",
        }
        try:
            response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error("Wetterdaten-Abruf fehlgeschlagen (%s, %s): %s", lat, lon, e)
            return None

    @staticmethod
    def analyze_wind(stream_data, weather_info, ride):
        """
        Berechnet den durchschnittlichen Gegenwind für eine Fahrt.

        Nutzt den zur Startzeit passenden Wetterwert statt immer Index 0.
        """
        latlngs = stream_data.get("latlng", {}).get("data", [])
        hourly = weather_info.get("hourly", {})
        wind_directions = hourly.get("wind_direction_10m", [])
        wind_speeds = hourly.get("wind_speed_10m", [])
        times = hourly.get("time", [])

        if not latlngs or not wind_directions:
            return 0.0

        wind_index = find_hourly_index(times, ride.start_date)

        w_dir = wind_directions[wind_index]
        w_speed = wind_speeds[wind_index]

        heading = calculate_heading(
            latlngs[0][0], latlngs[0][1], latlngs[-1][0], latlngs[-1][1]
        )
        return calculate_headwind(heading, w_dir, w_speed)
