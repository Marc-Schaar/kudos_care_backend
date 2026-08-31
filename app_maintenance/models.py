from datetime import date
from typing import TYPE_CHECKING

from django.db import models
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator

from app_auth.models import StravaProfile

if TYPE_CHECKING:
    from django.db.models.fields.related_descriptors import RelatedManager

    from app_dashboard.models import Ride


class BikeType(models.TextChoices):
    ROAD = "road", "Rennrad"
    MTB = "mtb", "Mountainbike"
    GRAVEL = "gravel", "Gravel"
    EBIKE_ROAD = "ebike_road", "E-Rennrad"
    EBIKE_MTB = "ebike_mtb", "E-MTB"
    EBIKE_CITY = "ebike_city", "E-Stadtrad"
    CITY = "city", "Stadtrad"
    CX = "cx", "Cyclocross"
    TRIATHLON = "triathlon", "Triathlon/Zeitfahrrad"
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
    ACCESSORIES = "accessories", "Zubehör"
    OTHER = "other", "Sonstiges"


class MaintenanceKind(models.TextChoices):
    """
    Unterscheidet physische Verschleißteile (`PART`, werden als ComponentSlot +
    Component instanziiert und getauscht) von Verbrauchsmaterial/wiederkehrender
    Pflege (`CONSUMABLE`, z.B. Kettenschmierung, Tubeless-Dichtmilch, Bremse
    entlüften). Consumables haben keinen prozentualen "Zustand", sondern werden
    über `MaintenanceInterval` als Intervall getrackt und per "Erledigt"-Log
    zurückgesetzt.
    """

    PART = "part", "Verschleißteil"
    CONSUMABLE = "consumable", "Verbrauchsmaterial / Pflege"


class WarnLevel:
    OK = "ok"
    WARN = "warn"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


def warn_status_from_ratio(ratio: float | None) -> str:
    """
    Gemeinsame Ampel-Logik (siehe auch api/serializers.py::WarnStatus, das hierher
    delegiert): ratio = aktueller Wert / Grenzwert.
    < 0.8 → ok, 0.8–1.0 → warn, ≥ 1.0 → critical, None → unknown.
    """
    if ratio is None:
        return WarnLevel.UNKNOWN
    if ratio >= 1.0:
        return WarnLevel.CRITICAL
    if ratio >= 0.8:
        return WarnLevel.WARN
    return WarnLevel.OK


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

    # Zwischengespeicherter KI-Zustandsbericht über alle montierten Komponenten
    # (siehe app_maintenance/api/services.py::build_bike_condition_report_prompt).
    condition_report = models.TextField(
        blank=True,
        default="",
        help_text="Zwischengespeicherte KI-generierte Zusammenfassung (Deutsch) des Gesamtzustands des Bikes.",
    )
    condition_report_generated_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Zeitpunkt der letzten Zustandsbericht-Generierung.",
    )

    # Dedupe fuer die "voraussichtlich unsafe bei naechster Fahrt"-E-Mail (siehe
    # app_notifications). Speichert, fuer welches vorhergesagte Datum zuletzt gewarnt wurde,
    # damit nicht taeglich erneut gemeldet wird solange die Vorhersage stabil bleibt.
    predicted_unsafe_notified_for_date = models.DateField(
        null=True,
        blank=True,
        help_text="Vorhergesagtes Next-Ride-Datum, fuer das zuletzt eine Vorhersage-Warn-E-Mail verschickt wurde.",
    )

    class Meta:
        ordering = ["name"]

    if TYPE_CHECKING:
        id: int
        rides: RelatedManager["Ride"]
        slots: RelatedManager["ComponentSlot"]

        def get_bike_type_display(self) -> str: ...

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


class ComponentGroup(models.Model):
    """
    Baugruppe im Katalog, die mehrere ComponentTemplates verbindet, die
    physisch typischerweise zusammen gewechselt werden (z.B. "Laufrad vorne"
    umfasst Reifen/Felge/Nabenlager/Speichen/Felgenband vorne). Grundlage für
    den Quick-Change-Flow (siehe app_maintenance/api/views.py::SlotQuickChangeView).
    Bewusst generisch gehalten — weitere Gruppen (z.B. "Bremse vorne") lassen
    sich rein über den Admin anlegen, ohne Codeänderung.

    Dient als Blueprint: pro Bike wird daraus eine `BikeAssembly`-Instanz
    (mit eigenem Namen/Verlauf) erzeugt.
    """

    name = models.CharField(max_length=100)
    notes = models.TextField(blank=True)
    category = models.CharField(
        max_length=20,
        choices=ComponentCategory.choices,
        default=ComponentCategory.OTHER,
        help_text="Bestimmt Icon/Farbe/Sortierung in der UI.",
    )
    applicable_bike_types = models.JSONField(
        default=list,
        blank=True,
        help_text="Liste von BikeType-Keys. Leer = gilt für alle.",
    )
    sort_order = models.PositiveIntegerField(default=0)
    is_system = models.BooleanField(
        default=True,
        help_text="True = aus Migration/Seed, False = vom User angelegt.",
    )
    recommended = models.BooleanField(
        default=True,
        help_text="Im geführten Stepper für ein neues Bike vorausgewählt.",
    )

    class Meta:
        ordering = ["sort_order", "name"]

    if TYPE_CHECKING:
        id: int
        templates: RelatedManager["ComponentTemplate"]
        instances: RelatedManager["BikeAssembly"]

    def __str__(self):
        return self.name

    def applies_to(self, bike_type: str) -> bool:
        if not self.applicable_bike_types:
            return True
        return bike_type in self.applicable_bike_types


class ComponentTemplate(models.Model):
    """
    Vordefinierter Katalog aller möglichen Verschleißkomponenten.
    Wird als Fixture geliefert, kann aber auch vom User erweitert werden.
    """

    name = models.CharField(max_length=100)
    category = models.CharField(max_length=20, choices=ComponentCategory.choices)
    group = models.ForeignKey(
        ComponentGroup,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="templates",
        help_text="Optionale Baugruppe für den Quick-Change (z.B. 'Laufrad vorne').",
    )
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
    maintenance_kind = models.CharField(
        max_length=20,
        choices=MaintenanceKind.choices,
        default=MaintenanceKind.PART,
        help_text=(
            "PART = physisches Verschleißteil (Slot + Component). "
            "CONSUMABLE = Verbrauchsmaterial/Pflege, wird als MaintenanceInterval "
            "getrackt statt als Component."
        ),
    )
    default_in_group = models.BooleanField(
        default=True,
        help_text="Im Baugruppen-Dialog vorausgewählt (seltene Teile: False).",
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["category", "name"]

    if TYPE_CHECKING:
        id: int
        slots: RelatedManager["ComponentSlot"]

        def get_category_display(self) -> str: ...

    def __str__(self):
        return f"{self.get_category_display()} — {self.name}"

    def applies_to(self, bike_type: str) -> bool:
        """Gibt True zurück wenn diese Vorlage für den Fahrradtyp gilt."""
        if not self.applicable_bike_types:
            return True
        return bike_type in self.applicable_bike_types


class BikeAssembly(models.Model):
    """
    Instanz einer Baugruppe (ComponentGroup) an einem konkreten Bike, z.B.
    "Laufrad vorne" oder – vom User umbenannt – "Sommer-LRS". Bündelt die
    zugehörigen `ComponentSlot`s. Pro `(bike, group)` darf höchstens eine
    Instanz aktiv sein (`is_active`, erzwungen in `clean()`); inaktive Instanzen
    bleiben als Wechsel-Historie erhalten. Gleichzeitige Baugruppen
    unterschiedlicher `group` sind natürlich erlaubt.
    """

    bike = models.ForeignKey(
        Bike,
        on_delete=models.CASCADE,
        related_name="assemblies",
    )
    group = models.ForeignKey(
        ComponentGroup,
        on_delete=models.PROTECT,
        related_name="instances",
    )
    name = models.CharField(
        max_length=100,
        blank=True,
        help_text="Anzeigename, Default = group.name. Editierbar (z.B. 'Sommer-LRS').",
    )
    installed_at = models.DateField(
        null=True,
        blank=True,
        help_text="Wann diese Baugruppe (zuletzt komplett) montiert wurde.",
    )
    retired_at = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["group__sort_order", "group__name", "-created_at"]
        verbose_name = "Baugruppe (Bike-Instanz)"
        verbose_name_plural = "Baugruppen (Bike-Instanzen)"

    if TYPE_CHECKING:
        id: int
        slots: RelatedManager["ComponentSlot"]
        intervals: RelatedManager["MaintenanceInterval"]

    def __str__(self):
        return f"{self.bike.name} — {self.display_name}"

    @property
    def display_name(self) -> str:
        return self.name or self.group.name

    def clean(self):
        if self.is_active:
            qs = BikeAssembly.objects.filter(
                bike=self.bike, group=self.group, is_active=True
            ).exclude(pk=self.pk)
            if qs.exists():
                raise ValidationError(
                    f"Für '{self.group.name}' an diesem Bike ist bereits eine "
                    "Baugruppe aktiv. Bitte zuerst die aktuelle deaktivieren."
                )

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def compute_km(self, bike_total_km: float | None) -> float | None:
        """
        Gefahrene km seit dieser Baugruppe = bike_total minus dem km-Stand beim
        Einbau des ältesten noch montierten Teils der Baugruppe.
        """
        if bike_total_km is None:
            return None
        installs = [
            c.distance_at_install
            for slot in self.slots.all()
            for c in slot.components.all()
            if c.is_mounted and c.distance_at_install is not None
        ]
        if not installs:
            return None
        return round(bike_total_km - min(installs), 1)

    def worst_status(self, bike_total_km: float | None) -> str:
        """Schlechtester Status über montierte Komponenten + zugehörige Intervalle."""
        from .api.serializers import compute_wear

        statuses: list[str] = []
        for slot in self.slots.all():
            comp = slot.mounted_component
            if comp is None:
                statuses.append(WarnLevel.UNKNOWN)
                continue
            statuses.append(compute_wear(comp, bike_total_km)["warn_status_overall"])
        for interval in self.intervals.all():
            statuses.append(interval.status(bike_total_km))

        for level in (
            WarnLevel.CRITICAL,
            WarnLevel.WARN,
            WarnLevel.OK,
            WarnLevel.UNKNOWN,
        ):
            if level in statuses:
                return level
        return WarnLevel.UNKNOWN


class ComponentSlot(models.Model):
    """
    Ein logischer Steckplatz am Fahrrad, z.B. 'Kette', 'Reifen vorne'.
    Mehrere Component-Instanzen können einem Slot zugeordnet sein,
    aber immer nur eine ist montiert (is_mounted=True).

    Gehört (fast immer) zu einer `BikeAssembly`; `assembly=None` nur für
    Alt-Slots, die noch keiner Baugruppe zugeordnet wurden. `bike` bleibt
    denormalisiert gesetzt (== assembly.bike, falls vorhanden).
    """

    bike = models.ForeignKey(
        Bike,
        on_delete=models.CASCADE,
        related_name="slots",
    )
    assembly = models.ForeignKey(
        BikeAssembly,
        on_delete=models.CASCADE,
        related_name="slots",
        null=True,
        blank=True,
    )
    template = models.ForeignKey(
        ComponentTemplate,
        on_delete=models.PROTECT,
        related_name="slots",
    )
    # Freitext-Override falls der User den Slot umbenennen möchte
    custom_name = models.CharField(max_length=100, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["assembly", "template"],
                condition=models.Q(assembly__isnull=False),
                name="uniq_assembly_template",
            ),
            models.UniqueConstraint(
                fields=["bike", "template"],
                condition=models.Q(assembly__isnull=True),
                name="uniq_bike_template_ungrouped",
            ),
        ]
        ordering = ["template__category", "template__name"]

    if TYPE_CHECKING:
        id: int
        components: RelatedManager["Component"]

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

    # Wetter-gewichteter Verschleiß (siehe app_maintenance/api/services.py::WeatherWearService)
    weather_wear_km = models.FloatField(
        null=True,
        blank=True,
        help_text=(
            "Wetter-gewichteter Verschleiß in km seit Einbau, inkl. Zuschlägen für "
            "Regen/Hitze/Kälte/Wind. Wird asynchron per Celery-Task neu berechnet, "
            "nicht live pro Request."
        ),
    )
    weather_wear_computed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Zeitpunkt der letzten Neuberechnung von weather_wear_km.",
    )
    weather_wear_ride_count = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=(
            "Anzahl der in die letzte Berechnung einbezogenen Fahrten seit Einbau — "
            "dient als Konfidenz-Indikator (wenige Fahrten = wenig aussagekräftiger Wert)."
        ),
    )
    weather_wear_explanation = models.TextField(
        blank=True,
        default="",
        help_text="Zwischengespeicherte KI-generierte Erklärung (Deutsch) der Wetter-Verschleiß-Zahlen.",
    )
    weather_wear_explanation_generated_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "Zeitpunkt der letzten KI-Erklärung-Generierung. Wird gegen "
            "weather_wear_computed_at verglichen, um die Erklärung bei neuen Zahlen "
            "ungültig zu machen."
        ),
    )

    # Zwischengespeicherte KI-Anleitung "wie prüfe ich diese Komponente" (siehe
    # app_maintenance/api/services.py::build_check_instructions_prompt). Anders als bei
    # weather_wear_explanation gibt es keinen einzelnen "Quelldaten geändert"-Zeitstempel,
    # gegen den man vergleichen könnte — die Anleitung wird stattdessen ungültig, wenn sich
    # der Gesamt-Warn-Status seit der Generierung verändert hat (siehe
    # check_instructions_status).
    check_instructions = models.TextField(
        blank=True,
        default="",
        help_text="Zwischengespeicherte KI-generierte Prüfanleitung (Deutsch) für diese Komponente.",
    )
    check_instructions_status = models.CharField(
        max_length=20,
        blank=True,
        default="",
        help_text="warn_status_overall zum Zeitpunkt der letzten Anleitung-Generierung.",
    )
    check_instructions_generated_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Zeitpunkt der letzten Prüfanleitung-Generierung.",
    )

    # Dedupe fuer die Warn/Critical-E-Mail (siehe app_notifications). Speichert den zuletzt
    # per E-Mail gemeldeten warn_status_overall, damit derselbe Status nicht wiederholt
    # gemeldet wird, eine Eskalation (warn -> critical) oder ein erneutes Auftreten nach
    # einer Freigabe aber erneut eine E-Mail ausloest.
    last_warn_notified_status = models.CharField(
        max_length=20,
        blank=True,
        default="",
        help_text="warn_status_overall zum Zeitpunkt der letzten Warn-E-Mail.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-installed_at"]

    if TYPE_CHECKING:
        id: int
        checks: RelatedManager["ComponentCheck"]

    def __str__(self):
        status = "montiert" if self.is_mounted else "ausgebaut"
        label = f"{self.brand} {self.model_name}".strip() or "Unbekannt"
        return f"{label} [{status}] @ {self.slot}"

    @property
    def effective_warn_km(self) -> float | None:
        return (
            self.custom_warn_km
            if self.custom_warn_km is not None
            else self.slot.template.warn_km
        )

    @property
    def effective_warn_days(self) -> int | None:
        return (
            self.custom_warn_days
            if self.custom_warn_days is not None
            else self.slot.template.warn_days
        )

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
    checked_at_weather_wear_km = models.FloatField(
        null=True,
        blank=True,
        help_text=(
            "component.weather_wear_km zum Zeitpunkt der Prüfung — Baseline, damit "
            "der wetterbereinigte Warn-Status nach einer Freigabe ebenfalls zurückgesetzt "
            "wird (analog zu checked_at_distance_km für die km-Achse)."
        ),
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

    if TYPE_CHECKING:
        id: int

    def __str__(self):
        return f"Check {self.checked_at} @ {self.component}"


class MaintenanceIntervalKind(models.TextChoices):
    CHAIN_LUBE = "chain_lube", "Kettenschmierung"
    SEALANT = "sealant", "Tubeless-Dichtmilch"
    BRAKE_BLEED = "brake_bleed", "Bremse entlüften"
    DI2_CHARGE = "di2_charge", "Schaltungs-Akku laden"
    BATTERY = "battery", "Batterie/Akku"
    CUSTOM = "custom", "Sonstiges"


class MaintenanceInterval(models.Model):
    """
    Verbrauchsmaterial / wiederkehrende Pflege ohne "Zustand" (Kettenschmierung,
    Tubeless-Dichtmilch, Bremse entlüften, Di2/AXS laden). Bewusst leichter als
    ComponentSlot/Component: kein Tausch, nur ein "Erledigt"-Log
    (`MaintenanceLog`), das die km-/Tage-Baseline zurücksetzt.
    """

    bike = models.ForeignKey(
        Bike,
        on_delete=models.CASCADE,
        related_name="intervals",
    )
    assembly = models.ForeignKey(
        BikeAssembly,
        on_delete=models.CASCADE,
        related_name="intervals",
        null=True,
        blank=True,
        help_text="Optionale Zuordnung zu einer Baugruppe (z.B. Dichtmilch → Laufrad).",
    )
    template = models.ForeignKey(
        ComponentTemplate,
        on_delete=models.SET_NULL,
        related_name="intervals",
        null=True,
        blank=True,
        help_text="Herkunfts-Vorlage (maintenance_kind=CONSUMABLE), falls aus Katalog.",
    )
    kind = models.CharField(
        max_length=20,
        choices=MaintenanceIntervalKind.choices,
        default=MaintenanceIntervalKind.CUSTOM,
    )
    label = models.CharField(max_length=100)
    interval_km = models.FloatField(null=True, blank=True)
    interval_days = models.IntegerField(null=True, blank=True)
    last_done_at = models.DateField(null=True, blank=True)
    last_done_distance_km = models.FloatField(
        null=True,
        blank=True,
        help_text="Km-Stand des Bikes bei der letzten Erledigung.",
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["label"]
        verbose_name = "Wartungs-Intervall"
        verbose_name_plural = "Wartungs-Intervalle"

    if TYPE_CHECKING:
        id: int
        logs: RelatedManager["MaintenanceLog"]

    def __str__(self):
        return f"{self.bike.name} — {self.label}"

    def status(self, bike_total_km: float | None, as_of: date | None = None) -> str:
        """
        Ampel wie `compute_wear`: schlechtester Wert aus km- und Tage-Achse.
        `as_of` projiziert nur die Tage-Achse (Default: heute) — analog zur
        Fahrt-Vorhersage in app_notifications.
        """
        as_of = as_of or date.today()
        ratios: list[float] = []

        if self.interval_km:
            if bike_total_km is not None and self.last_done_distance_km is not None:
                ratios.append(
                    (bike_total_km - self.last_done_distance_km) / self.interval_km
                )
            elif bike_total_km is not None:
                ratios.append(bike_total_km / self.interval_km)

        if self.interval_days and self.last_done_at:
            ratios.append((as_of - self.last_done_at).days / self.interval_days)

        if not ratios:
            return WarnLevel.UNKNOWN
        return warn_status_from_ratio(max(ratios))


class MaintenanceLog(models.Model):
    """Ein Eintrag "erledigt am" für ein MaintenanceInterval (Historie)."""

    interval = models.ForeignKey(
        MaintenanceInterval,
        on_delete=models.CASCADE,
        related_name="logs",
    )
    done_at = models.DateField(default=date.today)
    done_distance_km = models.FloatField(null=True, blank=True)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-done_at", "-id"]

    if TYPE_CHECKING:
        id: int

    def __str__(self):
        return f"{self.interval.label} erledigt {self.done_at}"


class WeatherSensitivityCoefficient(models.Model):
    """
    Pro-Kategorie-Gewichtung für den wetterbedingten Verschleiß-Multiplikator
    (siehe app_maintenance/api/services.py::WeatherWearCalculator). Als DB-Tabelle
    statt Python-Konstanten modelliert, damit eine zukünftige Kalibrierung aus
    echten Nutzerdaten (ComponentCheck.condition_pct-Verlauf) die Werte ohne
    Schema-Änderung aktualisieren kann. Ein Datensatz je ComponentCategory.
    """

    category = models.CharField(
        max_length=20, choices=ComponentCategory.choices, unique=True
    )
    rain_sensitivity = models.FloatField(
        default=0.0, help_text="Gewichtung des Regen-Faktors (0 = kein Effekt)."
    )
    heat_sensitivity = models.FloatField(
        default=0.0, help_text="Gewichtung des Hitze-Faktors (0 = kein Effekt)."
    )
    cold_sensitivity = models.FloatField(
        default=0.0, help_text="Gewichtung des Kälte-Faktors (0 = kein Effekt)."
    )
    wind_sensitivity = models.FloatField(
        default=0.0,
        help_text=(
            "Gewichtung des Wind-Faktors (0 = kein Effekt). Bewusst konservativ, da der "
            "Wind-Mechanismus für mechanischen Verschleiß physikalisch schwach ist."
        ),
    )
    last_calibrated_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Zeitpunkt der letzten Kalibrierung aus echten Nutzerdaten. Null = es gilt noch der Heuristik-Default.",
    )
    calibration_sample_count = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Anzahl Datenpunkte der letzten Kalibrierung. Null = noch nie kalibriert.",
    )
    notes = models.TextField(
        blank=True, default="", help_text="Begründung/Quelle der aktuellen Werte."
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["category"]
        verbose_name = "Wetter-Sensitivitäts-Koeffizient"
        verbose_name_plural = "Wetter-Sensitivitäts-Koeffizienten"

    if TYPE_CHECKING:
        id: int

        def get_category_display(self) -> str: ...

    def __str__(self):
        return self.get_category_display()
