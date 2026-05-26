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
        challenge = request.query_params.get('hub.challenge')
        if challenge:
            return Response({'hub.challenge': challenge}, status=status.HTTP_200_OK)
        return Response({'error': 'Invalid request'}, status=status.HTTP_400_BAD_REQUEST)

    def post(self, request):
        data = request.data
        process_strava_webhook.delay(data)
        return Response(status=status.HTTP_200_OK)