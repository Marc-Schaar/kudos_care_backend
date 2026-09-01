import json
import logging

from celery.result import AsyncResult
from django.core.serializers import serialize
from django.shortcuts import get_object_or_404
from django.utils import timezone

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import status

from .serializers import RideSerializer, StravaSyncStatusSerializer
from .services import build_ride_summary_prompt
from .tasks import run_strava_sync
from .wind import (
    compute_ride_wind,
    hourly_from_weather_data,
    segments_to_geojson,
)
from ..models import Ride
from app_auth.models import StravaProfile
from app_auth.mixins import CsrfExemptSessionAuthentication
from app_maintenance.api.ai_providers import generate_reviewed_text, get_ai_provider
from app_maintenance.api.services import (
    build_ride_wear_impact_prompt,
    ride_wear_breakdown,
    ride_wear_impact_is_stale,
)

logger = logging.getLogger("my_app_debug")


class StravaSyncView(APIView):
    """POST /api/strava/sync/ — Stößt einen asynchronen Strava-Sync an (Celery)."""

    authentication_classes = [CsrfExemptSessionAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        profile = get_object_or_404(
            StravaProfile,
            strava_athlete_id=request.session.get("strava_athlete_id"),
        )

        updated = StravaProfile.objects.filter(
            pk=profile.pk, sync_status__in=["idle", "success", "error", "cancelled"]
        ).update(
            sync_status="running",
            sync_started_at=timezone.now(),
            sync_error="",
            sync_progress_current=None,
            sync_progress_total=None,
        )

        if updated:
            async_result = run_strava_sync.delay(profile.pk)
            StravaProfile.objects.filter(pk=profile.pk).update(
                sync_task_id=async_result.id
            )

        return Response({"status": "running"}, status=status.HTTP_202_ACCEPTED)


class StravaSyncCancelView(APIView):
    """POST /api/strava/sync/cancel/ — Bricht einen laufenden Sync manuell ab."""

    authentication_classes = [CsrfExemptSessionAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        profile = get_object_or_404(
            StravaProfile,
            strava_athlete_id=request.session.get("strava_athlete_id"),
        )

        if profile.sync_status != "running":
            return Response({"status": profile.sync_status}, status=status.HTTP_200_OK)

        if profile.sync_task_id:
            AsyncResult(profile.sync_task_id).revoke(terminate=True)

        StravaProfile.objects.filter(pk=profile.pk, sync_status="running").update(
            sync_status="cancelled",
            sync_finished_at=timezone.now(),
            sync_error="Manuell abgebrochen",
        )
        return Response({"status": "cancelled"}, status=status.HTTP_200_OK)


class StravaSyncStatusView(APIView):
    """GET /api/strava/sync-status/ — Aktueller Sync-Status des eingeloggten Athleten."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        profile = get_object_or_404(
            StravaProfile,
            strava_athlete_id=request.session.get("strava_athlete_id"),
        )
        return Response(StravaSyncStatusSerializer(profile).data)


class ActivityListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        athlete_id = request.session.get("strava_athlete_id")
        rides = Ride.objects.filter(athlete__strava_athlete_id=athlete_id)
        serializer = RideSerializer(rides, many=True)
        return Response(serializer.data)


class ActivityDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, id):
        athlete_id = request.session.get("strava_athlete_id")
        ride = get_object_or_404(Ride, id=id, athlete__strava_athlete_id=athlete_id)
        geo_json = json.loads(serialize("geojson", [ride], geometry_field="track"))

        # Windabschnitte werden nicht persistiert (200 Abschnitte x Koordinaten wuerden
        # weather_data aufblaehen), sondern hier aus dem gespeicherten GPS-Stream plus
        # den Stundenwerten rekonstruiert — dieselbe Funktion wie beim Import, daher
        # per Konstruktion konsistent mit weather_data["avg_headwind"].
        hourly = hourly_from_weather_data(ride.weather_data)
        segments, wind_source = compute_ride_wind(ride, hourly)

        return Response(
            {
                "name": ride.name,
                "distance_km": (
                    round(ride.distance / 1000, 1) if ride.distance else None
                ),
                "elapsed_time": ride.elapsed_time,
                "start_date": ride.start_date,
                "bike_name": ride.bike.name if ride.bike else None,
                "geo_json_full": geo_json,
                "weather_timeline": ride.weather_data or {},
                "wind_segments": segments_to_geojson(segments, wind_source),
            }
        )


class ActivitySummaryView(APIView):
    """
    GET /api/activities/{id}/summary/?refresh=true

    Liefert eine gecachte, KI-generierte Zusammenfassung der Fahrt (Distanz,
    Dauer, Wetter, Gegenwind). Regeneriert nur wenn noch keine existiert oder
    ?refresh=true übergeben wurde — anders als beim Wetter-Verschleiß einer
    Komponente ändern sich die zugrunde liegenden Ride-Zahlen nach dem Import
    nicht mehr, es gibt daher keine separate Staleness-Prüfung. Die KI
    berechnet dabei nichts selbst — sie fasst nur bereits vorhandene Zahlen
    in Worten zusammen.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, id):
        athlete_id = request.session.get("strava_athlete_id")
        ride = get_object_or_404(Ride, id=id, athlete__strava_athlete_id=athlete_id)

        if ride.distance is None and ride.elapsed_time is None:
            return Response(
                {
                    "error": "Keine Fahrtdaten für eine Zusammenfassung vorhanden.",
                    "code": "no_data",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        force_refresh = request.query_params.get("refresh") == "true"
        if ride.ai_summary and not force_refresh:
            return Response(
                {
                    "summary": ride.ai_summary,
                    "generated_at": ride.ai_summary_generated_at,
                    "cached": True,
                }
            )

        system_prompt, user_prompt = build_ride_summary_prompt(ride)
        summary = get_ai_provider().generate_text(system_prompt, user_prompt)

        if summary is None:
            return Response(
                {
                    "error": "KI-Zusammenfassung aktuell nicht verfügbar. Bitte später erneut versuchen.",
                    "code": "ai_unavailable",
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        ride.ai_summary = summary
        ride.ai_summary_generated_at = timezone.now()
        ride.save(update_fields=["ai_summary", "ai_summary_generated_at"])

        return Response(
            {
                "summary": summary,
                "generated_at": ride.ai_summary_generated_at,
                "cached": False,
            }
        )


class ActivityWearImpactView(APIView):
    """
    GET /api/activities/{id}/wear-impact/?refresh=true

    Was diese eine Fahrt die Komponenten gekostet hat: je Kategorie der wetterbedingte
    Aufschlag (+X %) und je Komponente der Anteil an ihrer empfohlenen Lebensdauer.

    Die Zahlen kommen immer frisch aus `ride_wear_breakdown()` — nur der erklärende
    KI-Text ist gecacht (`Ride.wear_impact_summary`) und wird neu generiert, wenn sich
    die beteiligten Komponenten seither geändert haben (etwa weil ein Einbaudatum
    nachträglich korrigiert wurde) oder ?refresh=true übergeben wurde. Bleibt die KI
    aus, liefert der Endpoint trotzdem die berechneten Zahlen — nur ohne Erzähltext.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, id):
        athlete_id = request.session.get("strava_athlete_id")
        ride = get_object_or_404(Ride, id=id, athlete__strava_athlete_id=athlete_id)

        breakdown = ride_wear_breakdown(ride)
        if not breakdown["component_count"]:
            return Response(
                {
                    "error": "Für diese Fahrt sind keine Komponenten hinterlegt.",
                    "code": "no_data",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        force_refresh = request.query_params.get("refresh") == "true"
        needs_generation = force_refresh or ride_wear_impact_is_stale(ride)

        if not needs_generation:
            return Response(
                {
                    "breakdown": breakdown,
                    "summary": ride.wear_impact_summary,
                    "generated_at": ride.wear_impact_generated_at,
                    "cached": True,
                }
            )

        system_prompt, user_prompt = build_ride_wear_impact_prompt(ride, breakdown)
        summary = generate_reviewed_text(system_prompt, user_prompt)

        if summary is None:
            # Anders als bei den reinen Erklär-Endpoints ist der Text hier nur die
            # Zugabe — die Zahlen stehen für sich, also kein 503.
            return Response(
                {
                    "breakdown": breakdown,
                    "summary": "",
                    "generated_at": None,
                    "cached": False,
                    "ai_unavailable": True,
                }
            )

        ride.wear_impact_summary = summary
        ride.wear_impact_generated_at = timezone.now()
        ride.save(update_fields=["wear_impact_summary", "wear_impact_generated_at"])

        return Response(
            {
                "breakdown": breakdown,
                "summary": summary,
                "generated_at": ride.wear_impact_generated_at,
                "cached": False,
            }
        )
