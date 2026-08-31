from rest_framework import serializers


class StravaAuthSerializer(serializers.Serializer):
    code = serializers.CharField(required=True, write_only=True)
    scope = serializers.CharField(required=False, allow_blank=True, write_only=True)


class UserSettingsSerializer(serializers.Serializer):
    """
    Body fuer PATCH /api/strava/me/ — die im Frontend aenderbaren Nutzer-Einstellungen.

    `email` liegt am Django-`User`, `email_notifications_enabled` am `StravaProfile`;
    die View schreibt beides. Leerer String ist erlaubt und bedeutet "E-Mail entfernen"
    — dann verschickt `app_notifications.services.send_templated_email()` nichts mehr.
    """

    email = serializers.EmailField(required=False, allow_blank=True)
    email_notifications_enabled = serializers.BooleanField(required=False)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError(
                "Mindestens eines von 'email' oder 'email_notifications_enabled' angeben."
            )
        return attrs
