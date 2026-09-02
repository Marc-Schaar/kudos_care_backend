import logging

import requests
from django.conf import settings
from django.contrib.auth import login, logout
from django.contrib.auth.models import User
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import ensure_csrf_cookie
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from app_auth.mixins import CsrfExemptSessionAuthentication
from app_auth.models import StravaProfile
from app_notifications.tasks import send_welcome_email_task

from .serializers import StravaAuthSerializer, UserSettingsSerializer
from .utils import sync_bikes_from_strava

logger = logging.getLogger("my_app_debug")


class StravaAuthCallbackView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = [CsrfExemptSessionAuthentication]

    def post(self, request, *args, **kwargs):
        serializer = StravaAuthSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        code = serializer.validated_data["code"]
        scope = serializer.validated_data.get("scope", "")
        granted_scopes = {s.strip() for s in scope.split(",") if s.strip()}

        if scope and "activity:read_all" not in granted_scopes:
            return Response(
                {
                    "error": "scope_insufficient",
                    "message": "Zugriff auf private Aktivitäten wurde nicht erlaubt.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

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
                sync_bikes_from_strava(athlete_data, profile)
                send_welcome_email_task.delay(profile.id)

            login(request, profile.user)

            request.session["strava_athlete_id"] = profile.strava_athlete_id
            return Response(
                {
                    "status": "success",
                    "message": "Erfolgreich mit Strava verbunden!",
                    "athlete": {
                        "id": profile.strava_athlete_id,
                        "firstname": athlete_data.get("firstname", ""),
                        "lastname": athlete_data.get("lastname", ""),
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
    """
    GET    /api/strava/me/  — Session-Check plus die im Frontend anzeigbaren Einstellungen.
    PATCH  /api/strava/me/  — E-Mail und Benachrichtigungs-Schalter aendern.
    DELETE /api/strava/me/?confirm=true — Konto und alle Daten loeschen.

    Der echte Name bleibt bewusst aussen vor (wird nicht persistiert, siehe
    StravaAuthCallbackView). `needs_email` treibt den Dialog beim naechsten Login:
    ohne E-Mail-Adresse verschickt app_notifications gar nichts, der Nutzer merkt das
    aber nie von allein.
    """

    def _profile(self, request) -> StravaProfile | None:
        athlete_id = request.session.get("strava_athlete_id")
        if not athlete_id:
            return None
        return get_object_or_404(StravaProfile, strava_athlete_id=athlete_id)

    def _payload(self, profile: StravaProfile) -> dict:
        email = getattr(profile.user, "email", "") or ""
        return {
            "athlete_id": profile.strava_athlete_id,
            "email": email,
            "email_notifications_enabled": profile.email_notifications_enabled,
            "needs_email": not email,
        }

    @method_decorator(ensure_csrf_cookie)
    def get(self, request):
        profile = self._profile(request)
        if profile is None:
            return Response(
                {"error": "Nicht eingeloggt"}, status=status.HTTP_401_UNAUTHORIZED
            )
        return Response(self._payload(profile))

    def patch(self, request):
        profile = self._profile(request)
        if profile is None:
            return Response(
                {"error": "Nicht eingeloggt"}, status=status.HTTP_401_UNAUTHORIZED
            )

        serializer = UserSettingsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        user = profile.user
        had_email = bool(user and user.email)

        if "email" in data and user is not None:
            user.email = data["email"]
            user.save(update_fields=["email"])

        if "email_notifications_enabled" in data:
            profile.email_notifications_enabled = data["email_notifications_enabled"]
            profile.save(update_fields=["email_notifications_enabled"])

        # Bei Erstanmeldung ohne hinterlegte E-Mail lief die Willkommens-Mail ins
        # Leere (send_templated_email gibt dann still False zurueck). Sobald zum
        # ersten Mal eine Adresse da ist, holen wir sie nach.
        if (
            not had_email
            and user is not None
            and user.email
            and profile.welcome_email_sent_at is None
        ):
            send_welcome_email_task.delay(profile.id)

        profile.refresh_from_db()
        return Response(self._payload(profile))

    def delete(self, request):
        """
        Löscht das Konto samt aller Daten — Art. 17 DSGVO.

        Es reicht, den `User` zu löschen: `StravaProfile` hängt per
        OneToOne-CASCADE daran, `Bike` und `Ride` per FK-CASCADE am Profil, und
        alles Weitere (Slots, Components, Nutzungszeiträume, Checks, Streams)
        wiederum daran. Ein Profil ohne User wäre ein Datenrest ohne Zugang —
        deshalb der Weg über den User und nicht über das Profil.

        `?confirm=true` ist Pflicht. Ein versehentliches DELETE auf diese Route
        löscht jahrelange Fahrthistorie unwiederbringlich; eine Bestätigung nur
        im Client schützt nicht vor einem falsch abgesetzten Aufruf.
        """
        profile = self._profile(request)
        if profile is None:
            return Response(
                {"error": "Nicht eingeloggt"}, status=status.HTTP_401_UNAUTHORIZED
            )

        if request.query_params.get("confirm") != "true":
            return Response(
                {
                    "error": "Löschen muss bestätigt werden (?confirm=true). Alle "
                    "Fahrten, Bikes und Komponenten werden dabei entfernt.",
                    "code": "confirmation_required",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = profile.user
        athlete_id = profile.strava_athlete_id

        with transaction.atomic():
            if user is not None:
                user.delete()
            else:
                # Altbestand ohne verknüpften User: dann das Profil direkt.
                profile.delete()

        logout(request)
        request.session.flush()
        # Bewusst nur die Athleten-Id, keine Adresse — Logs leben länger und
        # sind breiter zugänglich als die Datenbank.
        logger.info("Konto des Athleten %s auf Wunsch gelöscht.", athlete_id)
        return Response(status=status.HTTP_204_NO_CONTENT)
