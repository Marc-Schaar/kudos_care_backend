import json
import logging

import requests
from django.core.serializers import serialize
from django.shortcuts import get_object_or_404
from django.conf import settings

from rest_framework import status
from rest_framework.authentication import BasicAuthentication, SessionAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .services import StravaImportService
from ..models import Ride
from .serializers import RideSerializer
from app_auth.models import StravaProfile
from app_auth.api.utils import get_valid_access_token
from app_auth.mixins import CsrfExemptSessionAuthentication
from app_maintenance.models import Bike
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
            response = requests.get(
                "https://www.strava.com/api/v3/athlete/activities",
                headers={"Authorization": f"Bearer {profile.access_token}"},
                params={"per_page": settings.STRAVA_SYNC_PAGE_SIZE},
                timeout=10,
            )
            
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            logger.error("Strava-Sync fehlgeschlagen: %s", e)
            return Response(
                {"error": "Verbindungsfehler zu Strava"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        athlete_data = response.json()
        logger.debug(f"DEBUG: Strava Athlete Response Type: {type(athlete_data)}")
        logger.debug(f"DEBUG: Strava Athlete Response Content: {athlete_data}")
        if isinstance(athlete_data, list):
            athlete_data = athlete_data[0] if athlete_data else {}
        bikes_data = athlete_data.get("bikes", [])


        for bike_info in bikes_data:
            Bike.objects.update_or_create(
                strava_id=bike_info["id"], 
                defaults={
                    "name": bike_info.get("name"),
                    "athlete": profile,
                    # weitere Felder wie 'frame_type' falls vorhanden
                },
            )
        activities = response.json()
        for activity in activities:
            StravaImportService.sync_activity_to_db(activity, profile)

        return Response(
            {"status": "Erfolgreich synchronisiert", "count": len(activities)}
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
        ride = get_object_or_404(Ride, id=id)
        geo_json = json.loads(serialize("geojson", [ride], geometry_field="track"))

        return Response(
            {
                "name": ride.name,
                "geo_json_full": geo_json,
                "weather_timeline": ride.weather_data or {},
            }
        )
