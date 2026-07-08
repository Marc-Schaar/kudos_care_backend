from datetime import date

from django.db import models
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator

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

    def distance_km_up_to(self, as_of: date) -> float:
        """
        Gefahrene km dieses Bikes bis einschließlich `as_of`. Fahrten mit
        einem anderen Bike sind über die FK-Filterung auf `self.rides`
        automatisch ausgeschlossen. Ohne passende Fahrten wird 0 geliefert
        (nicht None), damit das Feld sinnvoll vorbefüllt werden kann.
        """
        from django.db.models import Sum

        result = self.rides.filter(start_date__date__lte=as_of).aggregate(
            total=Sum("distance")
        )["total"]
        return (result or 0) / 1000  # Strava liefert Meter


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
    warn_km = models.FloatField(null=True, blank=True, help_text="Warnung nach X km")
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
    supports_condition_estimate = models.BooleanField(
        default=True,
        help_text=(
            "Ob eine prozentuale Zustandsschätzung sinnvoll ist "
            "(z.B. nein bei Lagern/Services)."
        ),
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

    # Individuelle Lebensdauer-Vorgaben — überschreiben die Template-Empfehlung
    custom_warn_km = models.FloatField(
        null=True, blank=True, help_text="Individuelle Lebensdauer in km"
    )
    custom_warn_days = models.IntegerField(
        null=True, blank=True, help_text="Individuelle Lebensdauer in Tagen"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-installed_at"]

    def __str__(self):
        status = "montiert" if self.is_mounted else "ausgebaut"
        label = f"{self.brand} {self.model_name}".strip() or "Unbekannt"
        return f"{label} [{status}] @ {self.slot}"

    @property
    def effective_warn_km(self) -> float | None:
        return self.custom_warn_km if self.custom_warn_km is not None else self.slot.template.warn_km

    @property
    def effective_warn_days(self) -> int | None:
        return self.custom_warn_days if self.custom_warn_days is not None else self.slot.template.warn_days

    def clean(self):
        """Stellt sicher dass pro Slot maximal eine Komponente montiert ist."""
        if self.is_mounted:
            qs = Component.objects.filter(slot=self.slot, is_mounted=True).exclude(
                pk=self.pk
            )
            if qs.exists():
                raise ValidationError(
                    f"Im Slot '{self.slot.display_name}' ist bereits eine "
                    "Komponente montiert. Bitte zuerst die aktuelle ausbauen."
                )

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)


class ComponentCheck(models.Model):
    """
    Protokolliert eine Prüfung/Freigabe einer Komponente durch den User.
    Erlaubt es, eine als überfällig markierte Komponente wieder freizugeben,
    optional mit Zustandsschätzung und einem kürzeren Snooze-Intervall bis
    zur nächsten Erinnerung.
    """

    component = models.ForeignKey(
        Component,
        on_delete=models.CASCADE,
        related_name="checks",
    )
    checked_at = models.DateField(default=date.today)
    checked_at_distance_km = models.FloatField(
        null=True,
        blank=True,
        help_text="Km-Stand des Fahrrads zum Zeitpunkt der Prüfung",
    )
    condition_pct = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="Geschätzter Restzustand in %, nur falls vom Komponententyp unterstützt",
    )
    snooze_km = models.FloatField(
        null=True,
        blank=True,
        help_text="Erneut warnen nach X weiteren km ab checked_at_distance_km",
    )
    snooze_days = models.IntegerField(
        null=True,
        blank=True,
        help_text="Erneut warnen nach X weiteren Tagen ab checked_at",
    )
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-checked_at", "-id"]

    def __str__(self):
        return f"Check {self.checked_at} @ {self.component}"
