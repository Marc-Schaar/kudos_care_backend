from typing import TYPE_CHECKING, TypedDict

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from app_auth.models import StravaProfile

from .crypto import decrypt_json, encrypt_json


class RadcomputerSettings(models.Model):
    """
    Einstellungen des Radcomputers je Athlet. Vorher nur im localStorage — damit an jedes
    Gerät gebunden und nach dem Leeren des Browsers weg. Das Frontend hält weiter eine
    lokale Kopie, damit der Radcomputer ohne Netz mit den letzten Werten startet.
    """

    class Theme(models.TextChoices):
        NIGHT = "night"
        DAY = "day"

    class Audio(models.TextChoices):
        OFF = "off"
        BEEP = "beep"
        SPEECH = "speech"

    class RoutingProvider(models.TextChoices):
        BROUTER = "brouter"
        OSRM = "osrm"

    class RoutingProfile(models.TextChoices):
        TREKKING = "trekking"
        FASTBIKE = "fastbike"

    profile = models.OneToOneField(
        StravaProfile, on_delete=models.CASCADE, related_name="radcomputer_settings"
    )
    theme = models.CharField(max_length=10, choices=Theme.choices, default=Theme.NIGHT)
    auto_pause = models.BooleanField(default=True)
    auto_pause_kmh = models.FloatField(
        default=3, validators=[MinValueValidator(0.5), MaxValueValidator(20)]
    )
    auto_pause_in_workout = models.BooleanField(default=True)
    ftp = models.PositiveSmallIntegerField(
        default=200, validators=[MinValueValidator(50), MaxValueValidator(600)]
    )
    audio = models.CharField(max_length=10, choices=Audio.choices, default=Audio.SPEECH)
    power_alerts = models.BooleanField(default=True)
    lap_announcements = models.BooleanField(default=True)
    map_tiles = models.BooleanField(default=True)
    heading_up = models.BooleanField(default=True)
    routing_provider = models.CharField(
        max_length=10, choices=RoutingProvider.choices, default=RoutingProvider.BROUTER
    )
    routing_profile = models.CharField(
        max_length=10, choices=RoutingProfile.choices, default=RoutingProfile.TREKKING
    )
    show_status_bar = models.BooleanField(default=True)
    show_controls = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Radcomputer-Einstellungen"
        verbose_name_plural = "Radcomputer-Einstellungen"

    def __str__(self) -> str:
        return f"Radcomputer-Einstellungen {self.profile_id}"


class PlaceData(TypedDict):
    name: str
    lat: float
    lon: float


class SavedPlace(models.Model):
    """
    Festes Ziel im Routenplaner („Zuhause", „Arbeit").

    Name und Koordinaten liegen **nur verschlüsselt** in `ciphertext` (siehe `crypto.py`):
    ein Ort namens „Zuhause" ist die Wohnadresse. Deshalb gibt es auch keinen
    Unique-Constraint auf den Namen und keine Suche danach — beides ginge nur über den
    Klartext. Die Eindeutigkeit je Name stellt die View her (wenige Einträge je Athlet).
    """

    profile = models.ForeignKey(
        StravaProfile, on_delete=models.CASCADE, related_name="saved_places"
    )
    ciphertext = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    if TYPE_CHECKING:
        id: int

    class Meta:
        ordering = ["created_at", "id"]
        verbose_name = "Gespeicherter Ort"
        verbose_name_plural = "Gespeicherte Orte"

    @property
    def data(self) -> PlaceData:
        return decrypt_json(self.ciphertext)

    @data.setter
    def data(self, value: PlaceData) -> None:
        self.ciphertext = encrypt_json(
            {"name": value["name"], "lat": value["lat"], "lon": value["lon"]}
        )

    def __str__(self) -> str:
        # Bewusst ohne Namen: __str__ landet in Admin, Logs und Fehlermeldungen.
        return f"Ort {self.id}"
