from django.conf import settings
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny
from .tasks import process_strava_webhook

class StravaWebhookView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        """
        Strava Validierung: Strava sendet einen GET Request, um die Callback-URL zu verifizieren.
        """
        mode = request.query_params.get('hub.mode')
        token = request.query_params.get('hub.verify_token')
        challenge = request.query_params.get('hub.challenge')
        if mode == 'subscribe' and token == settings.STRAVA_VERIFY_TOKEN and challenge:
            return Response({'hub.challenge': challenge}, status=status.HTTP_200_OK)
        return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)

    def post(self, request):
        data = request.data
        process_strava_webhook.delay(data)
        return Response(status=status.HTTP_200_OK)