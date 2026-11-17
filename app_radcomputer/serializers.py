import re

from rest_framework import serializers

from .models import RadcomputerSettings

MAX_DATA_PAGES = 8
MAX_FIELDS_PER_PAGE = 8
MAX_PAGE_NAME = 20
FIELD_KEY = re.compile(r"^[a-zA-Z][a-zA-Z0-9]{0,23}$")


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
            "bell_button",
            "map_style",
            "data_pages",
            "updated_at",
        ]
        read_only_fields = ["updated_at"]

    def validate_data_pages(self, value: object) -> list[dict]:
        """
        Nur die Form: höchstens 8 Seiten mit je 1–8 Feldern, Namen kurz, Schlüssel im
        Format der Client-Felder. Welche Schlüssel es gibt, weiß nur der Client.
        """
        if not isinstance(value, list):
            raise serializers.ValidationError("Erwartet eine Liste von Seiten.")
        if len(value) > MAX_DATA_PAGES:
            raise serializers.ValidationError(
                f"Höchstens {MAX_DATA_PAGES} Datenseiten."
            )
        pages = []
        for page in value:
            if not isinstance(page, dict):
                raise serializers.ValidationError("Jede Seite braucht name und fields.")
            name = page.get("name")
            fields = page.get("fields")
            if not isinstance(name, str) or not name.strip():
                raise serializers.ValidationError("Jede Seite braucht einen Namen.")
            if len(name.strip()) > MAX_PAGE_NAME:
                raise serializers.ValidationError(
                    f"Seitennamen höchstens {MAX_PAGE_NAME} Zeichen."
                )
            if (
                not isinstance(fields, list)
                or not 1 <= len(fields) <= MAX_FIELDS_PER_PAGE
            ):
                raise serializers.ValidationError(
                    f"Jede Seite hat 1 bis {MAX_FIELDS_PER_PAGE} Felder."
                )
            if not all(isinstance(f, str) and FIELD_KEY.match(f) for f in fields):
                raise serializers.ValidationError("Unbekanntes Feldformat.")
            pages.append({"name": name.strip(), "fields": fields})
        return pages


class SavedPlaceSerializer(serializers.Serializer):
    """Klartext-Sicht auf einen `SavedPlace`; ver-/entschlüsselt wird im Model."""

    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(max_length=80, trim_whitespace=True)
    lat = serializers.FloatField(min_value=-90, max_value=90)
    lon = serializers.FloatField(min_value=-180, max_value=180)


# Rad-Sportarten, die Strava beim Aktualisieren einer Aktivität annimmt.
RIDE_SPORT_TYPES = [
    "Ride",
    "GravelRide",
    "MountainBikeRide",
    "EBikeRide",
    "EMountainBikeRide",
    "VirtualRide",
]


class ActivityCompleteSerializer(serializers.Serializer):
    """
    Was sich beim Upload nicht mitgeben lässt und deshalb nachträglich per
    `PUT /activities/{id}` an Strava geht. Alles optional.
    """

    sport_type = serializers.ChoiceField(choices=RIDE_SPORT_TYPES, required=False)
    gear_id = serializers.CharField(max_length=50, required=False)
    hide_from_home = serializers.BooleanField(required=False)
