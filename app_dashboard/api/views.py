import json
import logging

import requests
from django.core.serializers import serialize
from django.shortcuts import get_object_or_404

from rest_framework import status
from rest_framework.authentication import BasicAuthentication, SessionAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from app_auth.models import StravaProfile
from .services import StravaImportService
from ..models import Ride

logger = logging.getLogger(__name__)

STRAVA_SYNC_PAGE_SIZE = 2  # Anzahl Aktivitäten pro API-Abruf

class CsrfExemptSessionAuthentication(SessionAuthentication):
    def enforce_csrf(self, request):
        return


class StravaBikesView(APIView):
    authentication_classes = [SessionAuthentication, BasicAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, athlete_id):
        if str(request.session.get("strava_athlete_id")) != str(athlete_id):
            return Response(
                {"error": "Zugriff verweigert"}, status=status.HTTP_403_FORBIDDEN
            )

        profile = get_object_or_404(StravaProfile, strava_athlete_id=athlete_id)

        try:
            response = requests.get(
                "https://www.strava.com/api/v3/athlete",
                headers={"Authorization": f"Bearer {profile.access_token}"},
                timeout=10,
            )
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            logger.error("Strava-Athlete-Abruf fehlgeschlagen (athlete %s): %s", athlete_id, e)
            return Response(
                {"error": "Verbindungsfehler zu Strava"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        bikes = response.json().get("bikes", [])
        return Response({"athlete_id": athlete_id, "bikes": bikes})


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
                params={"per_page": STRAVA_SYNC_PAGE_SIZE},
                timeout=10,
            )
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            logger.error("Strava-Sync fehlgeschlagen: %s", e)
            return Response(
                {"error": "Verbindungsfehler zu Strava"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        activities = response.json()
        for activity in activities:
            StravaImportService.sync_activity_to_db(activity, access_token=profile.access_token)

        return Response({"status": "Erfolgreich synchronisiert", "count": len(activities)})


class ActivityListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        rides = (
            Ride.objects.all()
            .values("id", "strava_id", "name", "distance", "start_date")
            .order_by("-start_date")
        )
        return Response(list(rides))


class ActivityDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, id):
        ride = get_object_or_404(Ride, id=id)
        geo_json = json.loads(serialize("geojson", [ride], geometry_field="track"))

        return Response({
            "name": ride.name,
            "geo_json_full": geo_json,
            "weather_timeline": ride.weather_data or {},
        })