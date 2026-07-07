import json
import logging

from django.core.serializers import serialize
from django.shortcuts import get_object_or_404
from django.utils import timezone

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import status

from .serializers import RideSerializer, StravaSyncStatusSerializer
from .tasks import run_strava_sync
from ..models import Ride
from app_auth.models import StravaProfile
from app_auth.mixins import CsrfExemptSessionAuthentication

logger = logging.getLogger('my_app_debug')



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
            pk=profile.pk, sync_status__in=["idle", "success", "error"]
        ).update(sync_status="running", sync_started_at=timezone.now(), sync_error="")

        if updated:
            run_strava_sync.delay(profile.pk)

        return Response({"status": "running"}, status=status.HTTP_202_ACCEPTED)


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

        return Response(
            {
                "name": ride.name,
                "distance_km": round(ride.distance / 1000, 1) if ride.distance else None,
                "elapsed_time": ride.elapsed_time,
                "start_date": ride.start_date,
                "bike_name": ride.bike.name if ride.bike else None,
                "geo_json_full": geo_json,
                "weather_timeline": ride.weather_data or {},
            }
        )
