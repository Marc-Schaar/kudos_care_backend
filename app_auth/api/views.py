import requests
from django.conf import settings
from django.contrib.auth import login, logout
from django.contrib.auth.models import User
from django.shortcuts import get_object_or_404

from rest_framework import status
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from app_auth.models import StravaProfile
from .serializers import StravaAuthSerializer

from app_auth.mixins import CsrfExemptSessionAuthentication




class StravaAuthCallbackView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = [CsrfExemptSessionAuthentication]

    def post(self, request, *args, **kwargs):
        serializer = StravaAuthSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        code = serializer.validated_data["code"]


        strava_url = "https://www.strava.com/oauth/token"
        payload = {
            "client_id": settings.STRAVA_CLIENT_ID,
            "client_secret": settings.STRAVA_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
        }

        try:
            response = requests.post(strava_url, data=payload)
            response_data = response.json()

            if response.status_code != 200:
                return Response(
                    {
                        "error": "Strava-Token-Austausch fehlgeschlagen",
                        "details": response_data,
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            athlete_data = response_data.get("athlete", {})

            profile, created = StravaProfile.objects.update_or_create(
                strava_athlete_id=athlete_data.get("id"),
                defaults={
                    "firstname": athlete_data.get("firstname", ""),
                    "lastname": athlete_data.get("lastname", ""),
                    "access_token": response_data.get("access_token"),
                    "refresh_token": response_data.get("refresh_token"),
                    "expires_at": response_data.get("expires_at"),
                },
            )

            if not profile.user:
                user, _ = User.objects.get_or_create(
                    username=f"strava_{athlete_data.get('id')}"
                )
                profile.user = user
                profile.save()

            login(request, profile.user)

            request.session["strava_athlete_id"] = profile.strava_athlete_id
            return Response(
                {
                    "status": "success",
                    "message": "Erfolgreich mit Strava verbunden!",
                    "athlete": {
                        "id": profile.strava_athlete_id,
                        "firstname": profile.firstname,
                        "lastname": profile.lastname,
                    },
                },
                status=status.HTTP_200_OK,
            )

        except requests.exceptions.RequestException as e:
            return Response(
                {"error": "Verbindungsfehler zur Strava API", "details": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class LogoutView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = [CsrfExemptSessionAuthentication]

    def post(self, request):
        logout(request)
        request.session.flush()
        return Response({"message": "Erfolgreich ausgeloggt"})


class CurrentUserView(APIView):
    def get(self, request):
        athlete_id = request.session.get("strava_athlete_id")

        if not athlete_id:
            return Response(
                {"error": "Nicht eingeloggt"}, status=status.HTTP_401_UNAUTHORIZED
            )

        profile = get_object_or_404(StravaProfile, strava_athlete_id=athlete_id)

        return Response(
            {"athlete_id": profile.strava_athlete_id, "firstname": profile.firstname}
        )
