from django.conf import settings
from django.contrib.auth import login
from django.http import Http404

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from app_auth.dev_auth import get_or_create_dev_profile
from app_auth.mixins import CsrfExemptSessionAuthentication


class DevLoginView(APIView):
    """
    Nur in DEBUG erreichbar (siehe urls.py — die Route wird nur bei settings.DEBUG
    registriert; die Prüfung hier ist eine zweite Absicherung falls die View trotzdem
    direkt importiert würde). Loggt einen festen Fake-Athleten ein, ohne echten
    Strava-OAuth-Roundtrip — siehe app_auth/dev_auth.py und das Management-Command
    seed_dev_data (app_maintenance) für passende Testdaten dazu.
    """

    permission_classes = [AllowAny]
    authentication_classes = [CsrfExemptSessionAuthentication]

    def post(self, request, *args, **kwargs):
        if not settings.DEBUG:
            raise Http404()

        profile = get_or_create_dev_profile()
        login(request, profile.user)
        request.session["strava_athlete_id"] = profile.strava_athlete_id

        return Response(
            {
                "status": "success",
                "message": "Dev-Login erfolgreich (Mock, kein echter Strava-Login).",
                "athlete": {
                    "id": profile.strava_athlete_id,
                    "firstname": "Dev",
                    "lastname": "User",
                },
            },
            status=status.HTTP_200_OK,
        )
