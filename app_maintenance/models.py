from django.db import models
from django.core.exceptions import ValidationError

from app_auth.models import StravaProfile


class BikeType(models.TextChoices):
    ROAD = "road", "Rennrad"
    MTB = "mtb", "Mountainbike"
    GRAVEL = "gravel", "Gravel"
    EBIKE_ROAD = "ebike_road", "E-Rennrad"
    EBIKE_MTB = "ebike_mtb", "E-MTB"
    EBIKE_CITY = "ebike_city", "E-Stadtrad"
    CITY = "city", "Stadtrad"
    CX = "cx", "Cyclocross"
    OTHER = "other", "Sonstiges"


class ComponentCategory(models.TextChoices):
    DRIVETRAIN = "drivetrain", "Antrieb"
    BRAKES = "brakes", "Bremsen"
    WHEELS = "wheels", "Laufräder"
    SUSPENSION = "suspension", "Federung"
    COCKPIT = "cockpit", "Cockpit"
    FRAME = "frame", "Rahmen & Lager"
    ELECTRIC = "electric", "E-Antrieb"
    LIGHTING = "lighting", "Beleuchtung"
    OTHER = "other", "Sonstiges"


class Bike(models.Model):
    athlete = models.ForeignKey(
        StravaProfile,
        on_delete=models.CASCADE,
        related_name="bikes",
    )
    strava_bike_id = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=255)
    bike_type = models.CharField(
        max_length=20,
        choices=BikeType.choices,
        default=BikeType.OTHER,
    )
    retired = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.get_bike_type_display()})"
    
    @property
    def total_distance_km(self) -> float | None:
        from django.db.models import Sum
        result = self.rides.aggregate(total=Sum("distance"))["total"]
        if result is None:
            return None
        return result / 1000  # Strava liefert Meter



class ComponentTemplate(models.Model):
    """
    Vordefinierter Katalog aller möglichen Verschleißkomponenten.
    Wird als Fixture geliefert, kann aber auch vom User erweitert werden.
    """

    name = models.CharField(max_length=100)
    category = models.CharField(max_length=20, choices=ComponentCategory.choices)
    applicable_bike_types = models.JSONField(
        default=list,
        help_text="Liste von BikeType-Keys. Leer = gilt für alle.",
    )
    # Verschleißgrenzen — alle optional, Frontend zeigt Warnung wenn erreicht
    warn_km = models.FloatField(
        null=True, blank=True, help_text="Warnung nach X km"
    )
    warn_hours = models.FloatField(
        null=True, blank=True, help_text="Warnung nach X Fahrstunden"
    )
    warn_days = models.IntegerField(
        null=True, blank=True, help_text="Warnung nach X Tagen (Kalender)"
    )
    is_system = models.BooleanField(
        default=True,
        help_text="True = aus Fixture, False = vom User angelegt",
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["category", "name"]

    def __str__(self):
        return f"{self.get_category_display()} — {self.name}"

    def applies_to(self, bike_type: str) -> bool:
        """Gibt True zurück wenn diese Vorlage für den Fahrradtyp gilt."""
        if not self.applicable_bike_types:
            return True
        return bike_type in self.applicable_bike_types


class ComponentSlot(models.Model):
    """
    Ein logischer Steckplatz am Fahrrad, z.B. 'Kette', 'Reifen vorne'.
    Mehrere Component-Instanzen können einem Slot zugeordnet sein,
    aber immer nur eine ist montiert (is_mounted=True).
    """

    bike = models.ForeignKey(
        Bike,
        on_delete=models.CASCADE,
        related_name="slots",
    )
    template = models.ForeignKey(
        ComponentTemplate,
        on_delete=models.PROTECT,
        related_name="slots",
    )
    # Freitext-Override falls der User den Slot umbenennen möchte
    custom_name = models.CharField(max_length=100, blank=True)

    class Meta:
        # Pro Fahrrad darf jeder Template-Slot nur einmal existieren
        unique_together = [("bike", "template")]
        ordering = ["template__category", "template__name"]

    def __str__(self):
        return f"{self.bike.name} — {self.display_name}"

    @property
    def display_name(self) -> str:
        return self.custom_name or self.template.name

    @property
    def mounted_component(self):
        return self.components.filter(is_mounted=True).first()

    @property
    def wear_km(self) -> float | None:
        """Gefahrene km seit Einbau der montierten Komponente."""
        comp = self.mounted_component
        if comp is None or comp.distance_at_install is None:
            return None
        total_km = self.bike.athlete.stravaprofile_set  # wird unten überschrieben
        # Gesamtkilometer des Bikes aus Strava — hier als Property auf Bike ergänzt
        if self.bike.total_distance_km is None:
            return None
        return self.bike.total_distance_km - comp.distance_at_install


class Component(models.Model):
    """
    Eine konkrete physische Komponente, die einem Slot zugeordnet ist.
    Mehrere können einem Slot gehören (Ersatzreifen, alte Kette etc.),
    aber is_mounted=True darf nur einmal pro Slot vorkommen.
    """

    slot = models.ForeignKey(
        ComponentSlot,
        on_delete=models.CASCADE,
        related_name="components",
    )
    brand = models.CharField(max_length=100, blank=True)
    model_name = models.CharField(max_length=100, blank=True)

    # Kilometerstand des Fahrrads zum Einbauzeitpunkt (aus Strava)
    distance_at_install = models.FloatField(
        null=True,
        blank=True,
        help_text="Km-Stand des Fahrrads beim Einbau",
    )
    installed_at = models.DateField(null=True, blank=True)
    retired_at = models.DateField(
        null=True,
        blank=True,
        help_text="Datum des Ausbaus / Entsorgung",
    )
    is_mounted = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-installed_at"]

    def __str__(self):
        status = "montiert" if self.is_mounted else "ausgebaut"
        label = f"{self.brand} {self.model_name}".strip() or "Unbekannt"
        return f"{label} [{status}] @ {self.slot}"

    def clean(self):
        """Stellt sicher dass pro Slot maximal eine Komponente montiert ist."""
        if self.is_mounted:
            qs = Component.objects.filter(
                slot=self.slot, is_mounted=True
            ).exclude(pk=self.pk)
            if qs.exists():
                raise ValidationError(
                    f"Im Slot '{self.slot.display_name}' ist bereits eine "
                    "Komponente montiert. Bitte zuerst die aktuelle ausbauen."
                )

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)