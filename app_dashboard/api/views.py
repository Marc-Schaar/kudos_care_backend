import requests
import json
from django.core.serializers import serialize
from django.shortcuts import get_object_or_404

from rest_framework.authentication import SessionAuthentication, BasicAuthentication
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from app_auth.models import StravaProfile
from .services import StravaImportService
from ..models import Ride

PAGE_COUNT = 1


class CsrfExemptSessionAuthentication(SessionAuthentication):
    def enforce_csrf(self, request):
        return

class StravaBikesView(APIView):
    authentication_classes = [SessionAuthentication, BasicAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, athlete_id):
        try:
             profile = get_object_or_404(StravaProfile, strava_athlete_id=athlete_id)
        except StravaProfile.DoesNotExist:
            return Response({'error': 'Profil nicht gefunden.'}, status=status.HTTP_404_NOT_FOUND)
        
        if str(request.session.get('strava_athlete_id')) != str(athlete_id):
            return Response({'error': 'Zugriff verweigert'}, status=status.HTTP_403_FORBIDDEN)

        strava_url = "https://www.strava.com/api/v3/athlete"
        headers = {'Authorization': f'Bearer {profile.access_token}'}

        try:
            response = requests.get(strava_url, headers=headers)
            
            if response.status_code != 200:
                return Response({'error': 'Konnte Athlete-Daten von Strava nicht abrufen'}, status=response.status_code)
            
            athlete_data = response.json()
            
            bikes = athlete_data.get('bikes', [])

            return Response({
                'athlete_id': athlete_id,
                'bikes': bikes 
            }, status=status.HTTP_200_OK)

        except requests.exceptions.RequestException as e:
            return Response({'error': 'Verbindungsfehler zu Strava', 'details': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class StravaSyncView(APIView):
    authentication_classes = [CsrfExemptSessionAuthentication]
    permission_classes = [IsAuthenticated]

    """POST /api/sync/ - Lädt neue Aktivitäten von Strava"""
    def post(self, request):
        profile = get_object_or_404(StravaProfile, strava_athlete_id=request.session.get('strava_athlete_id'))
        
        strava_url = "https://www.strava.com/api/v3/athlete/activities"
        headers = {'Authorization': f'Bearer {profile.access_token}'}
        params = {'per_page': PAGE_COUNT} 
        
        response = requests.get(strava_url, headers=headers, params=params)
        
        if response.status_code == 200:
            activities = response.json()
            for activity in activities:
                StravaImportService.sync_activity_to_db(activity, access_token=profile.access_token)
            return Response({"status": "Erfolgreich synchronisiert", "count": len(activities)})
        
        return Response({'error': 'Sync fehlgeschlagen'}, status=response.status_code)



class ActivityListView(APIView):
    def get(self, request):
        rides = Ride.objects.all().values('id', 'strava_id', 'name', 'distance', 'start_date')
        return Response(list(rides))

class ActivityDetailView(APIView):
    def get(self, request, id):
        ride = get_object_or_404(Ride, id=id)
        geo_json_str = serialize('geojson', [ride], geometry_field='track')
        
        return Response({
            'name': ride.name,
            'geo_json_full': json.loads(geo_json_str),
            'weather_timeline': ride.weather_data if ride.weather_data else {}
        })