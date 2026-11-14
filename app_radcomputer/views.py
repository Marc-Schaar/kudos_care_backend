"""
Radcomputer: Fahrten zu Strava hochladen und gespeicherte Strava-Routen holen.

Nutzt den Strava-Zugang aus app_auth (Session-Login, Token-Refresh). Hochladen braucht
das Recht activity:write, das der normale Kudos-Login nicht anfragt; das Frontend holt
es auf Wunsch nach. Fehlt es, antworten die Endpunkte mit 403 und code=scope_missing.

Eine hochgeladene Fahrt landet über den Strava-Webhook automatisch auch in Kudos Care.
"""

import logging

import requests
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from app_auth.api.utils import strava_request
from app_auth.models import StravaProfile

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
