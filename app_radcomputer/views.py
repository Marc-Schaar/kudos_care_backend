"""
Radcomputer: Fahrten zu Strava hochladen und gespeicherte Strava-Routen holen.

Nutzt den Strava-Zugang aus app_auth (Session-Login, Token-Refresh). Hochladen braucht
das Recht activity:write. Der Kudos-Login fragt es mit an; Strava lässt Nutzer einzelne
Rechte aber abwählen. Fehlt es, antworten die Endpunkte mit 403 und code=scope_missing,
und das Frontend bietet das Nachholen an.

Dazu Einstellungen und gespeicherte Orte des Radcomputers (Orte nur verschlüsselt) sowie
der aktuelle Wind am Standort (Open-Meteo, nur mit Wetter-Einwilligung).

Eine hochgeladene Fahrt landet über den Strava-Webhook automatisch auch in Kudos Care.
"""

import logging

import requests
from django.core.cache import cache
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from app_auth.api.utils import strava_request
from app_auth.consent import has_weather_consent
from app_auth.models import StravaProfile

from .crypto import InvalidToken
from .models import RadcomputerSettings, SavedPlace
from .serializers import RadcomputerSettingsSerializer, SavedPlaceSerializer

logger = logging.getLogger("my_app_debug")

STRAVA_API = "https://www.strava.com/api/v3"
UPLOAD_TYPES = {"tcx", "tcx.gz", "gpx", "gpx.gz", "fit", "fit.gz"}


def _error(message: str, http_status: int, **extra) -> Response:
    return Response({"detail": message, **extra}, status=http_status)


class RadcomputerView(APIView):
    permission_classes = [IsAuthenticated]

    def get_athlete(self) -> StravaProfile:
        return get_object_or_404(
            StravaProfile,
            strava_athlete_id=self.request.session.get("strava_athlete_id"),
        )

    def strava(self, profile, method: str, path: str, **kwargs):
        """Aufruf mit Klartext-Fehlern statt Exceptions."""
        try:
            res = strava_request(profile, method, f"{STRAVA_API}{path}", **kwargs)
        except requests.HTTPError:
            # Refresh abgelehnt: Zugriff in Strava widerrufen.
            return None, _error(
                "Die Strava-Verbindung ist abgelaufen. Bitte neu anmelden.", 401
            )
        except requests.RequestException:
            logger.exception("Strava nicht erreichbar")
            return None, _error("Strava ist gerade nicht erreichbar.", 502)
        if res.status_code == 429:
            return None, _error(
                "Strava-Anfragelimit erreicht. Bitte in 15 Minuten erneut versuchen.",
                429,
            )
        return res, None


def _scope_missing() -> Response:
    return _error(
        "Für den Upload fehlt die Strava-Erlaubnis „Aktivitäten hochladen“.",
        status.HTTP_403_FORBIDDEN,
        code="scope_missing",
    )


class UploadView(RadcomputerView):
    """
    POST /api/radcomputer/upload/   multipart: file, data_type, name, commute, external_id

    Strava verarbeitet die Datei danach im Hintergrund; den Ausgang liefert
    GET /api/radcomputer/upload/<id>/.
    """

    parser_classes = [MultiPartParser]

    def post(self, request):
        profile = self.get_athlete()
        if not profile.has_scope("activity:write"):
            return _scope_missing()
        ride_file = request.FILES.get("file")
        if ride_file is None:
            return _error("Datei fehlt.", 400)
        data_type = str(request.data.get("data_type", "tcx")).lower()
        if data_type not in UPLOAD_TYPES:
            return _error(f"Unbekanntes Format: {data_type}", 400)

        res, err = self.strava(
            profile,
            "POST",
            "/uploads",
            data={
                "data_type": data_type,
                "name": str(request.data.get("name", "")).strip()[:255] or "Radfahrt",
                "commute": (
                    "1"
                    if str(request.data.get("commute", "0")) in ("1", "true")
                    else "0"
                ),
                "external_id": str(request.data.get("external_id", ride_file.name))[
                    :255
                ],
            },
            # Als Bytes, damit der zweite Versuch nach einem Token-Refresh dieselben Daten schickt.
            files={
                "file": (ride_file.name, ride_file.read(), "application/octet-stream")
            },
            timeout=60,
        )
        return err or _upload_response(res)


class UploadStatusView(RadcomputerView):
    """GET /api/radcomputer/upload/<id>/ — Stand der Verarbeitung bei Strava."""

    def get(self, request, upload_id: int):
        profile = self.get_athlete()
        if not profile.has_scope("activity:write"):
            return _scope_missing()
        res, err = self.strava(profile, "GET", f"/uploads/{upload_id}")
        return err or _upload_response(res)


def _upload_response(res) -> Response:
    try:
        body = res.json()
    except ValueError:
        body = {}
    if res.status_code >= 400:
        message = (
            body.get("error")
            or body.get("message")
            or f"Strava antwortet mit HTTP {res.status_code}."
        )
        return _error(str(message), res.status_code if res.status_code < 500 else 502)
    return Response(
        {
            "id": body.get("id"),
            "status": body.get("status", ""),
            "error": body.get("error"),
            "activity_id": body.get("activity_id"),
        },
        status=(
            status.HTTP_201_CREATED if res.status_code == 201 else status.HTTP_200_OK
        ),
    )


class RouteListView(RadcomputerView):
    """GET /api/radcomputer/routes/?page=1 — gespeicherte Routen, Radrouten zuerst."""

    def get(self, request):
        profile = self.get_athlete()
        try:
            page = max(1, int(request.query_params.get("page", 1)))
        except ValueError:
            page = 1
        res, err = self.strava(
            profile,
            "GET",
            f"/athletes/{profile.strava_athlete_id}/routes",
            params={"page": page, "per_page": 50},
        )
        if err:
            return err
        if res.status_code != 200:
            return _error(f"Strava antwortet mit HTTP {res.status_code}.", 502)
        routes = [
            {
                # id_str: Routen-IDs sind länger, als eine JS-Zahl genau abbilden kann.
                "id": r.get("id_str") or str(r.get("id")),
                "name": r.get("name") or "Route",
                "distance_m": r.get("distance") or 0,
                "elevation_m": r.get("elevation_gain") or 0,
                "ride": r.get("type") == 1,
                "private": bool(r.get("private")),
                "updated_at": r.get("updated_at"),
            }
            for r in res.json()
        ]
        routes.sort(key=lambda r: not r["ride"])
        return Response({"routes": routes, "page": page, "more": len(routes) == 50})


class RouteGpxView(RadcomputerView):
    """GET /api/radcomputer/routes/<id>/gpx/ — Route als GPX zum Nachfahren."""

    def get(self, request, route_id: str):
        profile = self.get_athlete()
        if not route_id.isdigit():
            return _error("Ungültige Routen-ID.", 400)
        res, err = self.strava(
            profile, "GET", f"/routes/{route_id}/export_gpx", timeout=30
        )
        if err:
            return err
        if res.status_code == 404:
            return _error("Route nicht gefunden oder privat ohne Freigabe.", 404)
        if res.status_code != 200:
            return _error(f"Strava antwortet mit HTTP {res.status_code}.", 502)
        return HttpResponse(
            res.content, content_type="application/gpx+xml; charset=utf-8"
        )


# --- Einstellungen und gespeicherte Orte ------------------------------------------------

MAX_PLACES = 50


class SettingsView(RadcomputerView):
    """
    GET /api/radcomputer/settings/  — gespeicherte Einstellungen; `stored: false` heißt,
                                      es gibt noch keine (dann die Standardwerte).
    PUT /api/radcomputer/settings/  — legt an oder aktualisiert. Teilweise Angaben sind
                                      erlaubt, damit ältere Clients ohne neue Felder nichts
                                      zurücksetzen.
    """

    def get(self, request):
        profile = self.get_athlete()
        instance = RadcomputerSettings.objects.filter(profile=profile).first()
        data = RadcomputerSettingsSerializer(
            instance or RadcomputerSettings(profile=profile)
        ).data
        return Response({**data, "stored": instance is not None})

    def put(self, request):
        profile = self.get_athlete()
        instance, _ = RadcomputerSettings.objects.get_or_create(profile=profile)
        serializer = RadcomputerSettingsSerializer(
            instance, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({**serializer.data, "stored": True})


def _place_payload(place: SavedPlace) -> dict | None:
    try:
        return {"id": place.id, **place.data}
    except (InvalidToken, ValueError, KeyError):
        # Falscher Schlüssel oder kaputter Eintrag: überspringen statt die Liste zu
        # sprengen. Ohne Namen loggen, der wäre Klartext.
        logger.error("Gespeicherter Ort %s nicht entschlüsselbar", place.id)
        return None


class SavedPlaceListView(RadcomputerView):
    """
    GET  /api/radcomputer/places/  — alle Orte, entschlüsselt.
    POST /api/radcomputer/places/  — {name, lat, lon}; ein Ort gleichen Namens wird ersetzt
                                     (wie bisher im Frontend), Antwort 201 bzw. 200.
    """

    def get(self, request):
        places = SavedPlace.objects.filter(profile=self.get_athlete())
        return Response([p for p in map(_place_payload, places) if p is not None])

    def post(self, request):
        profile = self.get_athlete()
        serializer = SavedPlaceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        existing = None
        places = list(SavedPlace.objects.filter(profile=profile))
        for place in places:
            payload = _place_payload(place)
            if payload and payload["name"] == data["name"]:
                existing = place
                break
        if existing is None and len(places) >= MAX_PLACES:
            return _error(f"Höchstens {MAX_PLACES} Orte speicherbar.", 400)

        place = existing or SavedPlace(profile=profile)
        place.data = data
        place.save()
        return Response(
            {"id": place.id, **place.data},
            status=status.HTTP_200_OK if existing else status.HTTP_201_CREATED,
        )


class SavedPlaceDetailView(RadcomputerView):
    """DELETE /api/radcomputer/places/<id>/"""

    def delete(self, request, place_id: int):
        place = get_object_or_404(SavedPlace, id=place_id, profile=self.get_athlete())
        place.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# --- Aktueller Wind ---------------------------------------------------------------------

OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"
# Zwei Nachkommastellen ≈ 1 km: reicht für ein Wettermodell mit km-Raster, und Open-Meteo
# erfährt nicht die genaue Position. Gleichzeitig der Cache-Schlüssel.
WIND_COORD_DIGITS = 2
# Open-Meteo rechnet die aktuellen Werte im 15-Minuten-Takt neu.
WIND_CACHE_SECONDS = 10 * 60
WIND_TIMEOUT_SECONDS = 8


def _coordinate(raw: str | None, limit: float) -> float | None:
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if value != value or abs(value) > limit:  # NaN oder außerhalb
        return None
    return round(value, WIND_COORD_DIGITS)


class WindView(RadcomputerView):
    """
    GET /api/radcomputer/wind/?lat=..&lon=..  — aktueller Wind am Standort.

    Antwort: {speed_kmh, gusts_kmh, direction_deg, observed_at, lat, lon}. `direction_deg`
    ist meteorologisch, also die Richtung, AUS der der Wind kommt (0 = Nord), wie in
    `app_dashboard/api/wind.py`. `lat`/`lon` sind die gerundeten Koordinaten, für die
    der Wert gilt.

    Live-Koordinaten gehen an einen Dritten, deshalb nur mit Wetter-Einwilligung (403
    `weather_consent_required`). Koordinaten werden nicht geloggt.
    """

    def get(self, request):
        profile = self.get_athlete()
        if not has_weather_consent(profile):
            return _error(
                "Für Winddaten fehlt die Einwilligung in die Wetterdaten.",
                status.HTTP_403_FORBIDDEN,
                code="weather_consent_required",
            )
        lat = _coordinate(request.query_params.get("lat"), 90)
        lon = _coordinate(request.query_params.get("lon"), 180)
        if lat is None or lon is None:
            return _error("lat und lon fehlen oder sind ungültig.", 400)

        key = f"rc-wind:{lat:.{WIND_COORD_DIGITS}f}:{lon:.{WIND_COORD_DIGITS}f}"
        payload = cache.get(key)
        if payload is None:
            payload = _fetch_wind(lat, lon)
            if payload is None:
                return _error("Winddaten sind gerade nicht verfügbar.", 502)
            cache.set(key, payload, WIND_CACHE_SECONDS)
        return Response(payload)


def _fetch_wind(lat: float, lon: float) -> dict | None:
    try:
        res = requests.get(
            OPEN_METEO_FORECAST,
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "wind_speed_10m,wind_direction_10m,wind_gusts_10m",
                "wind_speed_unit": "kmh",
                "timezone": "GMT",
            },
            timeout=WIND_TIMEOUT_SECONDS,
        )
        res.raise_for_status()
        current = res.json().get("current") or {}
    except (requests.RequestException, ValueError, AttributeError) as exc:
        logger.warning(
            "Open-Meteo (aktueller Wind) fehlgeschlagen: %s", type(exc).__name__
        )
        return None

    speed = current.get("wind_speed_10m")
    direction = current.get("wind_direction_10m")
    if not isinstance(speed, (int, float)) or not isinstance(direction, (int, float)):
        logger.warning("Open-Meteo lieferte keinen aktuellen Wind")
        return None
    gusts = current.get("wind_gusts_10m")
    observed = current.get("time")
    return {
        "speed_kmh": float(speed),
        "gusts_kmh": float(gusts) if isinstance(gusts, (int, float)) else None,
        "direction_deg": float(direction) % 360,
        # timezone=GMT: Open-Meteo liefert die Zeit ohne Zonenangabe.
        "observed_at": f"{observed}Z" if isinstance(observed, str) else None,
        "lat": lat,
        "lon": lon,
    }
