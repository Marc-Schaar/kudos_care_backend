from rest_framework import serializers

from .models import RadcomputerSettings


class RadcomputerSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = RadcomputerSettings
        fields = [
            "theme",
            "auto_pause",
            "auto_pause_kmh",
            "auto_pause_in_workout",
            "ftp",
            "audio",
            "power_alerts",
            "lap_announcements",
            "map_tiles",
            "heading_up",
            "routing_provider",
            "routing_profile",
            "show_status_bar",
            "show_controls",
            "updated_at",
        ]
        read_only_fields = ["updated_at"]


class SavedPlaceSerializer(serializers.Serializer):
    """Klartext-Sicht auf einen `SavedPlace`; ver-/entschlüsselt wird im Model."""

    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(max_length=80, trim_whitespace=True)
    lat = serializers.FloatField(min_value=-90, max_value=90)
    lon = serializers.FloatField(min_value=-180, max_value=180)
