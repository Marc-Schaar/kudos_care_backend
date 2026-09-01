import logging
import statistics
from datetime import date, timedelta

import polyline
import requests
from shapely.geometry import LineString as ShapelyLineString

from django.conf import settings
from django.contrib.gis.geos import LineString as DjangoLineString, Point
from django.db.models import F

from ..models import Ride, RideStream
from .wind import (
    average_headwind,
    compute_ride_wind,
    get_filtered_weather,
)
from app_auth.models import StravaProfile
from app_auth.api.utils import strava_get
from app_maintenance.models import Bike
from app_maintenance.api.tasks import recompute_weather_wear_for_bike

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 10

# Fahrt-Vorhersage (siehe predict_next_ride_date / app_notifications) — reine
# Heuristik auf Basis der juengsten Ride-Historie eines Bikes, keine ML-Prognose.
LOOKBACK_RIDE_COUNT = 10
MIN_RIDES_FOR_PREDICTION = 4


class StravaSyncService:
    @staticmethod
    def sync_bikes(profile: StravaProfile):
        """Holt die Bikes vom /athlete Endpunkt und synchronisiert sie."""
        try:
            resp = strava_get(
                profile,
                "https://www.strava.com/api/v3/athlete",
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()

            for bike_info in data.get("bikes", []):
                Bike.objects.update_or_create(
                    strava_bike_id=bike_info["id"],
                    defaults={"name": bike_info.get("name"), "athlete": profile},
                )
            logger.info(
                f"Bikes für {profile.strava_athlete_id} erfolgreich synchronisiert."
            )
        except Exception as e:
            logger.error(f"Fehler beim Bike-Sync für {profile.strava_athlete_id}: {e}")
            raise

    MAX_SYNC_PAGES = 50

    @staticmethod
    def estimate_new_activity_count(profile: StravaProfile):
        """
        Schätzt die Anzahl noch zu importierender Aktivitäten: Strava-Gesamtzahl
        (aus /athletes/{id}/stats, Summe aller Sportarten-Totals) abzüglich der
        bereits in der DB vorhandenen Rides dieses Athleten. Liefert None, wenn
        die Gesamtzahl nicht ermittelt werden konnte (z.B. Strava-Fehler) –
        der Fortschritt wird dann ohne Gesamtzahl (nur laufender Zähler) angezeigt.
        """
        try:
            resp = strava_get(
                profile,
                f"https://www.strava.com/api/v3/athletes/{profile.strava_athlete_id}/stats",
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            stats = resp.json()
        except requests.exceptions.RequestException as e:
            logger.warning(
                "Konnte Strava-Gesamtzahl für Athlet %s nicht ermitteln: %s",
                profile.strava_athlete_id,
                e,
            )
            return None

        total_on_strava = sum(
            (stats.get(key) or {}).get("count", 0)
            for key in ("all_ride_totals", "all_run_totals", "all_swim_totals")
        )
        already_synced = Ride.objects.filter(athlete=profile).count()
        return max(total_on_strava - already_synced, 0)

    @classmethod
    def sync_activities(cls, profile: StravaProfile):
        """Holt die komplette Aktivitäten-Historie (paginiert) und synchronisiert sie."""
        try:
            total_count = 0
            page = 1

            while page <= cls.MAX_SYNC_PAGES:
                resp = strava_get(
                    profile,
                    "https://www.strava.com/api/v3/athlete/activities",
                    params={"per_page": settings.STRAVA_SYNC_PAGE_SIZE, "page": page},
                    timeout=REQUEST_TIMEOUT,
                )
                resp.raise_for_status()
                activities = resp.json()

                if not activities:
                    break

                for activity in activities:
                    try:
                        ride = StravaImportService.sync_activity_to_db(
                            activity, profile
                        )
                        if ride is not None:
                            # Nur tatsächlich neu importierte Aktivitäten zählen für
                            # den Fortschritt – bereits synchronisierte werden von
                            # sync_activity_to_db mit None übersprungen.
                            StravaProfile.objects.filter(pk=profile.pk).update(
                                sync_progress_current=F("sync_progress_current") + 1
                            )
                    except Exception as e:
                        logger.error(
                            "Import fehlgeschlagen für Aktivität %s: %s",
                            activity.get("id"),
                            e,
                        )

                total_count += len(activities)

                if len(activities) < settings.STRAVA_SYNC_PAGE_SIZE:
                    break

                page += 1

            return total_count
        except requests.exceptions.RequestException as e:
            logger.error(f"Fehler beim Activity-Sync: {e}")
            raise

    @classmethod
    def full_sync(cls, profile: StravaProfile):
        """Kombinierter Sync-Prozess."""
        cls.sync_bikes(profile)

        total_new = cls.estimate_new_activity_count(profile)
        StravaProfile.objects.filter(pk=profile.pk).update(
            sync_progress_current=0, sync_progress_total=total_new
        )

        count = cls.sync_activities(profile)
        return count


class StravaImportService:
    def sync_activity_to_db(activity_data, profile):
        """
        Wandelt Strava-JSON in ein Ride-Objekt um und speichert es in PostGIS.
        """

        strava_id = activity_data["id"]
        if Ride.objects.filter(strava_id=strava_id).exists():
            return None

        polyline_str = activity_data.get("map", {}).get("summary_polyline")
        start_date_local = activity_data.get("start_date_local") or ""
        start_date = start_date_local.split("T")[0] if start_date_local else None
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

        gear_id = activity_data.get("gear_id")
        bike = (
            Bike.objects.filter(strava_bike_id=gear_id, athlete=profile).first()
            if gear_id
            else None
        )

        ride, created = Ride.objects.update_or_create(
            strava_id=activity_data["id"],
            defaults={
                "name": activity_data.get("name"),
                "track": track,
                "start_latlng": point,
                "distance": activity_data.get("distance"),
                "start_date": activity_data.get("start_date_local"),
                "elapsed_time": activity_data.get("elapsed_time"),
                "athlete": profile,
                "bike": bike,
            },
        )

        try:
            stream_data = StravaStreamService.fetch_activity_streams(
                ride.strava_id, profile
            )
        except requests.exceptions.RequestException as e:
            logger.error(
                "Stream-Abruf fehlgeschlagen für Ride %s: %s", ride.strava_id, e
            )
            stream_data = None

        avg_headwind = None
        if start_latlng and start_date:
            weather_info = WeatherService.get_historical_weather(
                start_latlng[0], start_latlng[1], start_date
            )
            # Ein einziger Segment-Durchlauf speist beides: den Stunden-Zeitverlauf
            # fuers Chart und den Durchschnitt fuer die Kopfzeile. Dadurch koennen
            # Text, Chart und Karte gar nicht mehr auseinanderlaufen.
            segments, _wind_source = compute_ride_wind(
                ride, (weather_info or {}).get("hourly") or {}, stream_data
            )
            avg_headwind = average_headwind(segments)
            ride.weather_data = {
                **get_filtered_weather(ride, weather_info, segments),
                "avg_headwind": avg_headwind,
            }
            ride.save()

            if ride.bike_id:
                recompute_weather_wear_for_bike.delay(ride.bike_id)

        if stream_data:
            RideStream.objects.update_or_create(
                ride=ride,
                defaults={
                    "latlngs": stream_data.get("latlng", {}).get("data"),
                    "time_series": stream_data.get("time", {}).get("data"),
                    "avg_headwind": avg_headwind,
                },
            )

        return ride


class StravaStreamService:
    @staticmethod
    def fetch_activity_streams(activity_id, profile):
        url = f"https://www.strava.com/api/v3/activities/{activity_id}/streams"
        params = {"keys": "latlng,time", "key_by_type": "true"}

        response = strava_get(profile, url, params=params, timeout=REQUEST_TIMEOUT)
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


def _mean(values):
    values = [v for v in (values or []) if v is not None]
    return round(sum(values) / len(values), 1) if values else None


def build_ride_summary_prompt(ride: Ride) -> tuple[str, str]:
    """
    Baut (system_prompt, user_prompt) fuer die KI-Zusammenfassung einer Fahrt.
    Fasst nur bereits vorhandene Zahlen (Distanz/Dauer/Wetter/Gegenwind) in
    Worten zusammen — die KI berechnet nichts selbst.
    """
    weather = ride.weather_data or {}
    distance_km = round(ride.distance / 1000, 1) if ride.distance else None
    duration_min = round(ride.elapsed_time / 60) if ride.elapsed_time else None

    system_prompt = (
        "Du bist ein Assistent für eine Fahrrad-Wartungs-App mit Strava-Integration. Du "
        "bekommst bereits fertig berechnete Kennzahlen zu einer einzelnen Fahrt (Distanz, "
        "Dauer, Wetter, Gegenwind). Fasse die Fahrt in 2-3 kurzen, allgemeinverständlichen "
        "Sätzen auf Deutsch zusammen, wie ein kurzer Rückblick. Nutze ausschließlich die "
        "gegebenen Zahlen — führe KEINE eigenen Berechnungen durch und erfinde KEINE "
        "zusätzlichen Werte oder Statistiken. Fehlende Werte einfach weglassen, nicht "
        "erwähnen. Halte den Ton sachlich, aber freundlich. Antworte nur mit dem Text, "
        "ohne Einleitung, ohne Anführungszeichen."
    )
    user_prompt = (
        f"Name der Fahrt: {ride.name}\n"
        f"Fahrrad: {ride.bike.name if ride.bike else 'unbekannt'}\n"
        f"Datum: {ride.start_date}\n"
        f"Distanz: {distance_km} km\n"
        f"Dauer: {duration_min} Minuten\n"
        f"Durchschnittstemperatur: {_mean(weather.get('temperature_2m'))} °C\n"
        f"Durchschnittlicher Niederschlag: {_mean(weather.get('precipitation'))} mm/h\n"
        f"Durchschnittliche Windgeschwindigkeit: {_mean(weather.get('wind_speed_10m'))} km/h\n"
        f"Durchschnittlicher Gegenwind: {weather.get('avg_headwind')} km/h\n"
    )
    return system_prompt, user_prompt


def predict_next_ride_date(bike: Bike) -> date | None:
    """
    Schätzt, wann dieses Bike voraussichtlich wieder gefahren wird — Basis für die
    "voraussichtlich unsafe bei nächster Fahrt"-Benachrichtigung (siehe
    app_maintenance.api.services.get_predicted_unsafe_bikes). Reine Heuristik: Median
    der Tage-Lücken zwischen den letzten `LOOKBACK_RIDE_COUNT` Fahrten dieses Bikes,
    angewandt auf die letzte Fahrt. Median statt Mittelwert, da robuster gegen einzelne
    Ausreißer (z.B. eine lange Winterpause).

    Gibt None zurück, wenn zu wenig Historie vorliegt (`MIN_RIDES_FOR_PREDICTION`) für
    eine halbwegs verlässliche Schätzung.
    """
    ride_dates = list(
        Ride.objects.filter(bike=bike, start_date__isnull=False)
        .order_by("-start_date")
        .values_list("start_date", flat=True)[:LOOKBACK_RIDE_COUNT]
    )
    if len(ride_dates) < MIN_RIDES_FOR_PREDICTION:
        return None

    gaps_days = [
        (ride_dates[i] - ride_dates[i + 1]).days for i in range(len(ride_dates) - 1)
    ]
    median_gap = statistics.median(gaps_days)

    predicted = ride_dates[0].date() + timedelta(days=median_gap)
    # Ist der Nutzer laut eigenem Muster schon "überfällig" fürs Fahren, ist "heute" der
    # sinnvollste Vorhersage-Punkt (keine Rückwärts-Projektion für compute_wear).
    return max(predicted, date.today())


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
