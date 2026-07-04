import json
import logging


from django.core.serializers import serialize
from django.shortcuts import get_object_or_404

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import status

from .services import StravaSyncService
from ..models import Ride
from .serializers import RideSerializer
from app_auth.models import StravaProfile
from app_auth.mixins import CsrfExemptSessionAuthentication
import logging
logger = logging.getLogger('my_app_debug')



class StravaSyncView(APIView):
    """POST /api/strava/sync/ — Lädt neue Aktivitäten von Strava und importiert sie."""

    authentication_classes = [CsrfExemptSessionAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        profile = get_object_or_404(
            StravaProfile,
            strava_athlete_id=request.session.get("strava_athlete_id"),
        )

        try:
            count = StravaSyncService.full_sync(profile)
            return Response({"status": "Erfolgreich", "count": count})
        except Exception as e:
            return Response(
                {"error": "Synchronisation fehlgeschlagen"}, 
                status=status.HTTP_502_BAD_GATEWAY
            )


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
