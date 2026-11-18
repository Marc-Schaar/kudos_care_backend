import logging
import statistics
from dataclasses import dataclass
from datetime import date, timedelta

import polyline
import requests
from django.conf import settings
from django.contrib.gis.geos import LineString as DjangoLineString
from django.contrib.gis.geos import Point
from django.db.models import F
from shapely.geometry import LineString as ShapelyLineString

from app_auth.api.utils import strava_get
from app_auth.consent import has_route_weather_consent, has_weather_consent
from app_auth.models import StravaProfile
from app_maintenance.api.tasks import recompute_weather_wear_for_bike
from app_maintenance.models import Bike

from ..models import Ride, RideStream
from .ride_rain import (
    SOURCE_ROUTE,
    build_ride_rain,
    merge_route_minutely,
    ride_precipitation,
    route_rain_locations,
    to_aware,
)
from .wind import (
    average_headwind,
    compute_ride_wind,
    get_filtered_weather,
)

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 10
HISTORICAL_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"

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
                # Der Name wird nur beim ersten Mal übernommen: sonst setzt jeder Sync
                # zurück, was der Nutzer in der App umbenannt hat.
                Bike.objects.update_or_create(
                    strava_bike_id=bike_info["id"],
                    defaults={"athlete": profile},
                    create_defaults={
                        "athlete": profile,
                        "name": bike_info.get("name") or "Unbenanntes Rad",
                    },
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
        if start_latlng and start_date and has_weather_consent(profile):
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
            ride_rain = WeatherService.get_ride_rain(
                ride, start_latlng[0], start_latlng[1], stream_data
            )
            if ride_rain:
                ride.weather_data["ride_rain"] = ride_rain
            ride.save()

        # Auch ohne Wetterdaten (Rolle, kein GPS): die Fahrt zählt für die Teile, und
        # auch für welche, die inzwischen im Regal liegen.
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
    rain = ride_precipitation(weather, ride.start_date, ride.elapsed_time)
    precipitation = round(rain, 1) if rain is not None else None

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
        f"Durchschnittlicher Niederschlag: {precipitation} mm/h\n"
        f"Durchschnittliche Windgeschwindigkeit: {_mean(weather.get('wind_speed_10m'))} km/h\n"
        f"Durchschnittlicher Gegenwind: {weather.get('avg_headwind')} km/h\n"
    )
    return system_prompt, user_prompt


@dataclass(frozen=True)
class RideRhythm:
    """
    Wie dieses Bike zuletzt gefahren wurde, als Grundlage für die Fahrt-Vorhersage.
    Reine Heuristik auf den letzten `LOOKBACK_RIDE_COUNT` Fahrten, kein ML.

    `next_ride_raw` ist die ungeklemmte Schätzung: liegt sie in der Vergangenheit, ist
    der Nutzer nach seinem eigenen Rhythmus "überfällig" (Winterpause, Urlaub). Die
    App sagt das dann so, statt jeden Tag aufs Neue "heute" zu behaupten.
    """

    rides_considered: int
    last_ride: date
    median_gap_days: float
    median_km: float | None
    next_ride_raw: date

    @property
    def next_ride(self) -> date:
        # Keine Rückwärts-Projektion: compute_wear(as_of=...) soll nie in die
        # Vergangenheit schauen, also ist "heute" der früheste sinnvolle Punkt.
        return max(self.next_ride_raw, date.today())

    @property
    def is_overdue(self) -> bool:
        return self.next_ride_raw < date.today()


def ride_rhythm(bike: Bike) -> RideRhythm | None:
    """
    Median der Tage-Lücken und der Distanzen über die letzten Fahrten dieses Bikes.
    Median statt Mittelwert, damit eine lange Pause oder eine einzelne Tagestour die
    Schätzung nicht verzieht. None bei zu wenig Historie (`MIN_RIDES_FOR_PREDICTION`).

    Eine Query für Datum und Strecke — vorher fragten `predict_next_ride_date` und
    `predict_next_ride_km` getrennt.
    """
    rows = list(
        Ride.objects.filter(bike=bike, start_date__isnull=False)
        .order_by("-start_date")
        .values_list("start_date", "distance")[:LOOKBACK_RIDE_COUNT]
    )
    if len(rows) < MIN_RIDES_FOR_PREDICTION:
        return None

    dates = [start for start, _ in rows]
    gaps_days = [(dates[i] - dates[i + 1]).days for i in range(len(dates) - 1)]
    median_gap = statistics.median(gaps_days)

    distances = [dist for _, dist in rows if dist is not None]
    median_km = (
        round(statistics.median(distances) / 1000, 1)
        if len(distances) >= MIN_RIDES_FOR_PREDICTION
        else None
    )

    last_ride = dates[0].date()
    return RideRhythm(
        rides_considered=len(rows),
        last_ride=last_ride,
        median_gap_days=median_gap,
        median_km=median_km,
        next_ride_raw=last_ride + timedelta(days=median_gap),
    )


def predict_next_ride_date(bike: Bike) -> date | None:
    """
    Wann dieses Bike voraussichtlich wieder gefahren wird (frühestens heute) — Basis
    für die "voraussichtlich unsafe bei nächster Fahrt"-Benachrichtigung (siehe
    app_maintenance.api.services.get_predicted_unsafe_bikes) und die Prognose im
    Dashboard. Details in `ride_rhythm()`.
    """
    rhythm = ride_rhythm(bike)
    return rhythm.next_ride if rhythm else None


def predict_next_ride_km(bike: Bike) -> float | None:
    """
    Schätzt die Strecke der nächsten Fahrt (Median, siehe `ride_rhythm()`).

    Ohne diesen Wert projizierte die Vorhersage-Mail nur die Tage-Achse, und die meisten
    Teile sind über km begrenzt — die Mail ging dadurch praktisch nie raus.
    """
    rhythm = ride_rhythm(bike)
    return rhythm.median_km if rhythm else None


class WeatherService:
    @staticmethod
    def get_historical_weather(lat, lon, date):
        """
        Ruft das historische Wetter für einen Punkt an einem Datum ab.

        `timezone=auto` ist Pflicht: `Ride.start_date` kommt aus Stravas
        `start_date_local` — Ortszeit, trotz des angehängten `Z`. Ohne den Parameter
        antwortet Open-Meteo in UTC, und das Wetterfenster jeder Fahrt lag um den
        UTC-Abstand daneben (im Winter eine, im Sommer zwei Stunden zu spät). Ein
        Schauer während der Fahrt fiel so regelmäßig aus dem Fenster. Mit `auto`
        liefert Open-Meteo die Stunden in Ortszeit ohne Zonenangabe, `_to_aware()`
        liest sie wie `start_date` als „UTC" — beide Seiten zählen dieselbe Uhr.
        """
        url = "https://archive-api.open-meteo.com/v1/archive"
        params = {
            "latitude": lat,
            "longitude": lon,
            "start_date": date,
            "end_date": date,
            "hourly": "temperature_2m,precipitation,wind_speed_10m,wind_direction_10m",
            "timezone": "auto",
        }
        try:
            response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error("Wetterdaten-Abruf fehlgeschlagen (%s, %s): %s", lat, lon, e)
            return None

    @staticmethod
    def get_ride_rain(ride, lat, lon, stream_data=None) -> dict | None:
        """
        Regen in 15-Minuten-Schritten für die Dauer der Fahrt.

        Mit `route_weather_consent_at` und GPS-Stream dort, wo die Fahrt gerade war, sonst
        am Startpunkt. Die Einwilligung wird hier geprüft, direkt vor dem Aufruf.

        Das Archiv kennt nur Stundenwerte, deshalb die Historical-Forecast-API. Nativ
        viertelstündlich ist sie in Mitteleuropa und Nordamerika, anderswo interpoliert
        Open-Meteo die Stundenwerte. Ein Fehlschlag kostet nur die Auflösung: Die Fahrt
        rechnet dann mit den Stundenwerten weiter.
        """
        start = to_aware(ride.start_date)
        if start is None or not ride.elapsed_time:
            return None

        if stream_data and has_route_weather_consent(ride.athlete):
            route_rain = WeatherService._get_route_rain(ride, start, stream_data)
            if route_rain:
                return route_rain

        forecasts = WeatherService._fetch_minutely(ride, start, [(lat, lon)])
        if not forecasts:
            return None
        return build_ride_rain(start, ride.elapsed_time, forecasts[0])

    @staticmethod
    def _get_route_rain(ride, start, stream_data) -> dict | None:
        locations = route_rain_locations(start, ride.elapsed_time, stream_data)
        places = list(dict.fromkeys((lat, lon) for _, lat, lon in locations))
        if not places:
            return None
        forecasts = WeatherService._fetch_minutely(ride, start, places)
        if not forecasts or len(forecasts) != len(places):
            return None
        minutely = merge_route_minutely(locations, dict(zip(places, forecasts)))
        return build_ride_rain(start, ride.elapsed_time, minutely, SOURCE_ROUTE)

    @staticmethod
    def _fetch_minutely(ride, start, places) -> list[dict] | None:
        """
        `minutely_15`-Blöcke in der Reihenfolge von `places`. Die Fehlermeldung bleibt aus
        dem Log, weil die URL darin die Koordinaten enthält.
        """
        end = start + timedelta(seconds=ride.elapsed_time)
        params = {
            "latitude": ",".join(str(lat) for lat, _ in places),
            "longitude": ",".join(str(lon) for _, lon in places),
            "start_date": start.date().isoformat(),
            "end_date": end.date().isoformat(),
            "minutely_15": "precipitation",
            "timezone": "auto",
        }
        try:
            response = requests.get(
                HISTORICAL_FORECAST_URL, params=params, timeout=REQUEST_TIMEOUT
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.exceptions.RequestException, ValueError):
            logger.warning("15-Minuten-Regen für Ride %s nicht abrufbar.", ride.pk)
            return None
        results = payload if isinstance(payload, list) else [payload]
        return [result.get("minutely_15") or {} for result in results]
