import logging
import math
import random
from datetime import timedelta

from django.conf import settings
from django.contrib.gis.geos import LineString, Point
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from app_auth.dev_auth import get_or_create_dev_profile
from app_dashboard.api.wind import (
    average_headwind,
    build_wind_segments,
    hourly_headwind,
)
from app_maintenance.api.services import WeatherWearService
from app_maintenance.models import (
    Bike,
    BikeAssembly,
    BikeType,
    Component,
    ComponentGroup,
    ComponentSlot,
    ComponentTemplate,
    MaintenanceInterval,
    MaintenanceKind,
)

logger = logging.getLogger("my_app_debug")

DEV_BIKE_STRAVA_ID = "dev-bike-1"
# Weit ausserhalb des Bereichs echter Strava-Activity-IDs, damit es nie kollidiert.
RIDE_STRAVA_ID_BASE = 900_000_000_000
# Grober Startpunkt (München) fuer die Fake-Tracks, geografisch nicht weiter relevant.
BASE_LAT, BASE_LNG = 48.137, 11.575

# Stuetzpunkte je Fake-Route. Genug, dass die distanzgleiche Segmentierung in
# app_dashboard/api/wind.py etwas zu tun hat, ohne die Dev-DB aufzublaehen.
ROUTE_POINTS = 240


def _loop_route(
    center_lat: float,
    center_lng: float,
    distance_km: float,
    elapsed_time: int,
    rng: random.Random,
):
    """
    Erzeugt eine geschlossene, leicht verbeulte Schleife als Fake-GPS-Stream.

    Eine Schleife (statt der frueheren Start-Ziel-Geraden) ist hier der ganze Punkt:
    nur wenn der Kurs sich ueber die Fahrt dreht, unterscheiden sich die
    Gegenwind-Werte der einzelnen Abschnitte — sonst waere die Karte lokal einfarbig
    und der Kurven-Fall gar nicht testbar.

    Der Radius wird aus `distance_km` abgeleitet, damit die gezeichnete Route ungefaehr
    so lang ist wie `Ride.distance` behauptet — sonst wundert man sich beim manuellen
    Nachrechnen ueber eine 8-km-Schleife bei einer 60-km-Fahrt.

    Gibt `(latlngs, time_series)` im selben Format zurueck, das Strava liefert:
    `[[lat, lon], ...]` plus Sekunden seit Start.
    """
    # Umfang = 2*pi*r, ein Grad Breite ~ 111.19 km.
    radius_lat = distance_km / (2 * math.pi * 111.19)
    radius_lng = radius_lat / math.cos(math.radians(center_lat))
    wobble = rng.uniform(0.1, 0.25)
    phase = rng.uniform(0.0, 2 * math.pi)

    latlngs = []
    for i in range(ROUTE_POINTS):
        angle = 2 * math.pi * i / (ROUTE_POINTS - 1)
        # Dritte Harmonische -> unrunde, realistischer wirkende Schleife.
        stretch = 1.0 + wobble * math.sin(3 * angle + phase)
        latlngs.append(
            [
                round(center_lat + radius_lat * stretch * math.cos(angle), 6),
                round(center_lng + radius_lng * stretch * math.sin(angle), 6),
            ]
        )

    step = elapsed_time / (ROUTE_POINTS - 1)
    time_series = [int(i * step) for i in range(ROUTE_POINTS)]
    return latlngs, time_series


class Command(BaseCommand):
    help = (
        "Erzeugt einen kompletten Fake-Datensatz (StravaProfile, Bike, montierte "
        "Komponenten, Ride-Historie mit Wetterdaten) fuer die lokale Entwicklung, ohne "
        "echten Strava-OAuth-Login oder echte Strava-API-Calls. Nur mit DEBUG=True lauffaehig. "
        "Login dazu ueber POST /api/dev/login/ (siehe app_auth/api/dev_views.py)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Vorhandenes Dev-Bike inkl. Slots/Components/Rides vorher loeschen statt zu aktualisieren.",
        )
        parser.add_argument(
            "--rides",
            type=int,
            default=20,
            help="Anzahl der zu erzeugenden Fake-Rides (Default 20).",
        )

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError(
                "seed_dev_data läuft nur mit DEBUG=True — nicht für Produktion gedacht."
            )

        from app_dashboard.models import Ride, RideStream

        reset = options["reset"]
        ride_count = options["rides"]
        rng = random.Random(42)

        profile = get_or_create_dev_profile()
        self.stdout.write(
            f"StravaProfile (athlete_id={profile.strava_athlete_id}) bereit."
        )

        if ComponentTemplate.objects.count() == 0:
            self.stdout.write(
                "Keine ComponentTemplates vorhanden, lade Fixture 'component_templates'..."
            )
            call_command("loaddata", "component_templates")

        if reset:
            Ride.objects.filter(bike__strava_bike_id=DEV_BIKE_STRAVA_ID).delete()
            # Bike-CASCADE räumt Slots/Components/Assemblies/Intervalle mit ab.
            Bike.objects.filter(strava_bike_id=DEV_BIKE_STRAVA_ID).delete()

        bike, _ = Bike.objects.update_or_create(
            strava_bike_id=DEV_BIKE_STRAVA_ID,
            defaults={
                "athlete": profile,
                "name": "Dev Testbike",
                "bike_type": BikeType.GRAVEL,
                "retired": False,
            },
        )
        self.stdout.write(f"Bike '{bike.name}' (id={bike.pk}) bereit.")

        # ── Baugruppen + montierte Komponenten + Wartungs-Intervalle ──────────
        # installed_at wird bewusst vor den aeltesten Fake-Ride gelegt (mit Streuung),
        # damit WeatherWearService beim Recompute unten auf eine sinnvolle Ride-Historie
        # trifft und der Verschleiss-Status je Komponente unterschiedlich ausfaellt
        # (manche ok, manche warn/critical) statt fuer alle identisch.
        max_ride_days_ago = ride_count * 4 + 2
        today = timezone.now().date()
        mounted = 0
        intervals_created = 0
        for group in ComponentGroup.objects.prefetch_related("templates"):
            if not group.applies_to(bike.bike_type):
                continue
            templates = [
                t for t in group.templates.all() if t.applies_to(bike.bike_type)
            ]
            if not templates:
                continue

            assembly, created = BikeAssembly.objects.get_or_create(
                bike=bike, group=group, is_active=True, defaults={"name": ""}
            )
            if not created:
                continue

            for template in templates:
                days_back = rng.randint(max_ride_days_ago + 15, max_ride_days_ago + 280)
                installed_at = today - timedelta(days=days_back)

                if template.maintenance_kind == MaintenanceKind.CONSUMABLE:
                    MaintenanceInterval.objects.create(
                        bike=bike,
                        assembly=assembly,
                        template=template,
                        label=template.name,
                        interval_km=template.warn_km,
                        interval_days=template.warn_days,
                        last_done_at=installed_at,
                        last_done_distance_km=0,
                    )
                    intervals_created += 1
                    continue

                slot, _ = ComponentSlot.objects.get_or_create(
                    assembly=assembly, template=template, defaults={"bike": bike}
                )
                if slot.mounted_component is not None:
                    continue
                Component.objects.create(
                    slot=slot,
                    brand="DevBrand",
                    model_name=template.name,
                    distance_at_install=0,
                    installed_at=installed_at,
                    is_mounted=True,
                )
                mounted += 1

            if assembly.installed_at is None:
                installs = [
                    c.installed_at
                    for s in assembly.slots.all()
                    for c in s.components.all()
                    if c.installed_at
                ]
                if installs:
                    assembly.installed_at = min(installs)
                    assembly.save(update_fields=["installed_at"])

            # Laufender Nutzungszeitraum, damit ein spaeterer Wechsel auf einen
            # zweiten Satz korrekt abgerechnet wird (siehe api/usage.py).
            assembly.ensure_open_period()

        self.stdout.write(
            f"{mounted} Komponenten montiert, {intervals_created} Wartungs-Intervalle angelegt."
        )

        # ── Fake-Rides mit Wetterdaten ─────────────────────────────────────────
        now = timezone.now()
        for i in range(ride_count):
            strava_id = RIDE_STRAVA_ID_BASE + i
            days_ago = (ride_count - i) * 4 + rng.randint(0, 2)
            start_date = now - timedelta(days=days_ago)
            distance_m = rng.uniform(15_000, 90_000)
            avg_speed_ms = rng.uniform(5.5, 8.5)
            elapsed_time = int(distance_m / avg_speed_ms)

            lat_offset = rng.uniform(-0.05, 0.05)
            lng_offset = rng.uniform(-0.05, 0.05)
            center_lat = BASE_LAT + lat_offset
            center_lng = BASE_LNG + lng_offset

            # Geschlossene Schleife statt der frueheren 2-Punkt-Geraden: nur damit
            # bekommt die Route ueberhaupt wechselnde Kurse, und nur dann zeigt die
            # Karte lokal etwas anderes als eine einfarbige Linie.
            latlngs, time_series = _loop_route(
                center_lat, center_lng, distance_m / 1000, elapsed_time, rng
            )
            start_point = Point(latlngs[0][1], latlngs[0][0], srid=4326)
            track = LineString([(lng, lat) for lat, lng in latlngs], srid=4326)

            is_rainy = rng.random() < 0.25
            is_cold = rng.random() < 0.2
            is_hot = rng.random() < 0.15
            is_windy = rng.random() < 0.2

            precipitation = [
                (
                    round(rng.uniform(2.0, 8.0), 1)
                    if is_rainy
                    else round(rng.uniform(0.0, 0.3), 1)
                )
                for _ in range(4)
            ]
            base_temp = (
                rng.uniform(2.0, 8.0)
                if is_cold
                else rng.uniform(29.0, 35.0) if is_hot else rng.uniform(12.0, 22.0)
            )
            temperature_2m = [
                round(base_temp + rng.uniform(-1.5, 1.5), 1) for _ in range(4)
            ]
            base_wind = rng.uniform(22.0, 35.0) if is_windy else rng.uniform(3.0, 15.0)
            wind_speed_10m = [
                round(base_wind + rng.uniform(-3.0, 3.0), 1) for _ in range(4)
            ]
            # Windrichtung dreht ueber die Fahrt leicht — ohne dieses Feld faellt die
            # Karte auf den groben Start-Ziel-Fallback zurueck (siehe
            # wind.hourly_from_weather_data).
            base_direction = rng.uniform(0.0, 360.0)
            wind_direction_10m = [
                round((base_direction + h * rng.uniform(-12.0, 12.0)) % 360, 1)
                for h in range(4)
            ]
            hourly_times = [
                (
                    start_date.replace(minute=0, second=0, microsecond=0)
                    + timedelta(hours=h)
                ).strftime("%Y-%m-%dT%H:00")
                for h in range(4)
            ]

            weather_data = {
                "time": hourly_times,
                "precipitation": precipitation,
                "temperature_2m": temperature_2m,
                "wind_speed_10m": wind_speed_10m,
                "wind_direction_10m": wind_direction_10m,
            }
            segments = build_wind_segments(
                latlngs, time_series, start_date, weather_data
            )
            weather_data["headwind"] = hourly_headwind(
                segments, weather_data, start_date
            )
            avg_headwind = average_headwind(segments)
            weather_data["avg_headwind"] = avg_headwind

            ride, _ = Ride.objects.update_or_create(
                strava_id=strava_id,
                defaults={
                    "name": f"Dev Ride #{i + 1}",
                    "track": track,
                    "start_latlng": start_point,
                    "weather_data": weather_data,
                    "distance": distance_m,
                    "start_date": start_date,
                    "elapsed_time": elapsed_time,
                    "athlete": profile,
                    "bike": bike,
                },
            )
            RideStream.objects.update_or_create(
                ride=ride,
                defaults={
                    "latlngs": latlngs,
                    "time_series": time_series,
                    "avg_headwind": avg_headwind,
                },
            )
        self.stdout.write(f"{ride_count} Fake-Rides angelegt/aktualisiert.")

        # ── Wetter-Verschleiss synchron nachrechnen ────────────────────────────
        # In Produktion passiert das per Celery-Task nach jedem Ride-Import — hier
        # synchron, weil im schlanken Dev-Setup ohne Redis/Celery-Worker sonst kein
        # weather_wear_km befuellt wuerde.
        recomputed = 0
        for component in Component.objects.filter(slot__bike=bike, is_mounted=True):
            WeatherWearService.recompute_component(component)
            recomputed += 1
        self.stdout.write(f"{recomputed} Komponenten weather_wear_km neu berechnet.")

        self.stdout.write(
            self.style.SUCCESS(
                f"Fertig. Login als Dev-Athlet (athlete_id={profile.strava_athlete_id}) via "
                "POST /api/dev/login/ (nur mit DEBUG=True verfügbar)."
            )
        )
