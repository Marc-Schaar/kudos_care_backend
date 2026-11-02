from collections.abc import Iterable
from datetime import date, datetime
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from app_auth.models import StravaProfile

from .api.usage import (
    RideLedger,
    assembly_km,
    interval_km_since_done,
    moment_for,
    ride_clock_now,
    start_of_day,
)

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
    Gemeinsame Ampel-Logik: ratio = aktueller Wert / Grenzwert.
    < 0.8 → ok, 0.8–1.0 → warn, ≥ 1.0 → critical, None → unknown.
    """
    if ratio is None:
        return WarnLevel.UNKNOWN
    if ratio >= 1.0:
        return WarnLevel.CRITICAL
    if ratio >= 0.8:
        return WarnLevel.WARN
    return WarnLevel.OK


#: Reihenfolge der Ampel-Aggregation — der schlechteste Wert gewinnt. `unknown`
#: steht bewusst hinter `ok`: ein Teil ohne Datenbasis soll die Bike-Ampel nicht
#: besser aussehen lassen, aber auch keine Warnung erfinden.
WARN_LEVEL_PRIORITY = (
    WarnLevel.CRITICAL,
    WarnLevel.WARN,
    WarnLevel.OK,
    WarnLevel.UNKNOWN,
)


def worst_of(statuses: "Iterable[str]") -> str:
    """
    Schlechtester Status einer Sammlung — die einzige Stelle, an der aus
    mehreren Ampeln eine wird (Achsen einer Komponente, Slots+Intervalle einer
    Baugruppe, Baugruppen eines Bikes). Leere Sammlung → `unknown`.
    """
    seen = set(statuses)
    for level in WARN_LEVEL_PRIORITY:
        if level in seen:
            return level
    return WarnLevel.UNKNOWN


class BikeQuerySet(models.QuerySet):
    def with_total_distance(self) -> "BikeQuerySet":
        """
        Annotiert die Gesamtdistanz (in Metern) aus der Ride-Historie, damit
        `Bike.total_distance_km` ohne eigene Query auskommt. Ohne die
        Annotation feuert das Property bei *jedem* Zugriff ein
        `aggregate(Sum)` — in den Listen-Serializern also einmal je Slot und
        je Intervall.
        """
        return self.annotate(_total_distance_m=models.Sum("rides__distance"))


class Bike(models.Model):
    athlete = models.ForeignKey(
        StravaProfile,
        on_delete=models.CASCADE,
        related_name="bikes",
    )
    # NULL = von Hand angelegt, ohne Strava-Bezug. Ein leerer String ginge wegen
    # `unique` nur ein einziges Mal über alle Nutzer hinweg gut.
    strava_bike_id = models.CharField(max_length=50, unique=True, null=True, blank=True)
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

    objects = BikeQuerySet.as_manager()

    class Meta:
        ordering = ["name"]

    if TYPE_CHECKING:
        id: int
        rides: RelatedManager["Ride"]
        slots: RelatedManager["ComponentSlot"]
        intervals: RelatedManager["MaintenanceInterval"]
        assemblies: RelatedManager["BikeAssembly"]

        def get_bike_type_display(self) -> str: ...

    def __str__(self):
        return f"{self.name} ({self.get_bike_type_display()})"

    @property
    def total_distance_km(self) -> float | None:
        """
        Gesamtkilometer aus der Ride-Historie.

        Nutzt die Annotation aus `Bike.objects.with_total_distance()`, falls
        das Queryset sie mitbringt — sonst eine eigene Aggregat-Query. Wer den
        Wert mehrfach braucht (Schleifen über Slots/Intervalle), zieht ihn in
        eine lokale Variable bzw. über `api/serializers.py::bike_total_km` aus
        dem Serializer-Context, statt ihn je Durchlauf erneut zu lesen.
        """
        from django.db.models import Sum

        if hasattr(self, "_total_distance_m"):
            total_m = self._total_distance_m
        else:
            total_m = self.rides.aggregate(total=Sum("distance"))["total"]
        if total_m is None:
            return None
        return total_m / 1000  # Strava liefert Meter


class MountPosition(models.TextChoices):
    """
    Wo am Rad ein Teil sitzt — als Feld, nicht als Wort im Namen.

    Vorher stand "vorne"/"hinten" ausschliesslich im Freitext-Namen und wurde
    an zwei unabhaengigen Stellen per String-Match wieder herausgelesen (im
    Backend gar nicht, im Frontend-Diagramm ueber `display_name.includes(...)`).
    Das hatte zwei Folgen: ein umbenannter Slot verlor seine Position (und
    landete im Diagramm auf dem Vorderrad, weil das der Fallback war), und die
    Kassette hatte "hinten" nie im Namen — sie sass nur wegen einer zweiten,
    unabhaengigen Regel hinten, waehrend Migration 0020 sie gruppenseitig nach
    "Laufrad hinten" verschob und ihre `category` weiter `drivetrain` blieb.
    Drei Quellen, keine davon massgeblich.
    """

    FRONT = "front", "Vorne"
    REAR = "rear", "Hinten"


class GroupKind(models.TextChoices):
    """
    Ob eine `ComponentGroup` eine echte Baugruppe ist oder nur ein Bereich.

    `ASSEMBLY` = physische Einheit, die man am Stueck ab- und aufzieht
    (Laufrad). Nur hier ergibt "Wechseln" (`activate`) einen Sinn, und nur hier
    darf es mehrere Instanzen je Bike geben: ein zweiter Satz im Keller ist ein
    realer Fall.

    `AREA` = Teile, die denselben Ort teilen, aber unabhaengig verschleissen —
    Bremsbelaege 3.000 km neben Bremsscheibe 15.000 km neben Bremsfluessigkeit
    730 Tage; Kette 2.000 km neben Kurbel 30.000 km. Niemand hat ein zweites
    komplettes "Cockpit", deshalb gibt es hoechstens eine Instanz je Bike. Zwei
    Belag-Saetze sind zwei Teile im selben Slot (siehe `Component`); dass die
    Belaege zum Laufrad passen muessen, regelt die Kopplung
    (`BikeAssembly.linked_components`), nicht eine zweite "Bremse".
    """

    ASSEMBLY = "assembly", "Baugruppe (am Stück wechselbar)"
    AREA = "area", "Bereich (Teile einzeln)"


class ComponentGroup(models.Model):
    """
    Baugruppe im Katalog, die mehrere ComponentTemplates verbindet, die
    physisch typischerweise zusammen gewechselt werden (z.B. "Laufrad vorne"
    umfasst Reifen/Felge/Nabenlager/Speichen/Felgenband vorne). Grundlage für
    den Baugruppen-Flow (siehe app_maintenance/api/views.py::BikeAssemblyListView
    und AssemblySwapView). Bewusst generisch gehalten — weitere Gruppen (z.B. "Bremse vorne") lassen
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
    position = models.CharField(
        max_length=10,
        choices=MountPosition.choices,
        blank=True,
        default="",
        help_text=(
            "Vorne/hinten als Feld statt als Wort im Namen. Leer = ohne Seite "
            "(Antrieb, Cockpit, ...). Siehe MountPosition."
        ),
    )
    kind = models.CharField(
        max_length=10,
        choices=GroupKind.choices,
        default=GroupKind.AREA,
        help_text=(
            "ASSEMBLY = am Stück wechselbar (Laufradsatz), AREA = Teile "
            "verschleißen unabhängig und werden einzeln getauscht. Steuert, ob "
            "activate/swap überhaupt angeboten werden. Siehe GroupKind."
        ),
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


class ComponentTemplateQuerySet(models.QuerySet):
    def visible_to(self, athlete: StravaProfile) -> "ComponentTemplateQuerySet":
        """Der gemeinsame Katalog plus die eigenen Vorlagen dieses Nutzers."""
        return self.filter(
            models.Q(owner__isnull=True, is_system=True) | models.Q(owner=athlete)
        )

    def catalog(self) -> "ComponentTemplateQuerySet":
        """Nur der gemeinsame Katalog, ohne Vorlagen einzelner Nutzer."""
        return self.filter(owner__isnull=True, is_system=True)


class ComponentTemplate(models.Model):
    """
    Vordefinierter Katalog aller möglichen Verschleißkomponenten.
    Wird als Fixture geliefert, kann aber auch vom User erweitert werden — eigene
    Vorlagen gehören genau einem Nutzer (`owner`).
    """

    name = models.CharField(max_length=100)
    owner = models.ForeignKey(
        StravaProfile,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="custom_templates",
        help_text="Besitzer einer eigenen Vorlage. Leer = gemeinsamer Katalog.",
    )
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
    position = models.CharField(
        max_length=10,
        choices=MountPosition.choices,
        blank=True,
        default="",
        help_text=(
            "Vorne/hinten als Feld statt als Wort im Namen — das Diagramm liest "
            "das hier statt den Anzeigenamen zu durchsuchen, sodass Umbenennen "
            "die Position nicht mehr kaputtmacht. Leer = ohne Seite. "
            "Siehe MountPosition."
        ),
    )
    follows_wheel = models.BooleanField(
        default=False,
        help_text=(
            "Sitzt nicht auf dem Laufrad, muss aber dazu passen (Bremsbelaege, "
            "Bremsscheiben). Der Client schlaegt diese Teile als Kopplung vor, wenn "
            "ein zweites Laufrad angelegt wird. Siehe BikeAssembly.linked_components."
        ),
    )
    notes = models.TextField(blank=True)

    objects = ComponentTemplateQuerySet.as_manager()

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


class AssemblyStatus(models.TextChoices):
    """
    Die drei Zustände einer Baugruppe. Stand früher als Kombination aus
    `is_active` + `retired_at` in zwei Feldern, was jede Lesestelle gezwungen
    hat, die Kodierung zu kennen (`is_active=False, retired_at=None` = geparkt).
    """

    ACTIVE = "active", "Aufgezogen"
    PARKED = "parked", "Geparkt"
    RETIRED = "retired", "Ausgemustert"


class BikeAssemblyQuerySet(models.QuerySet):
    def active(self) -> "BikeAssemblyQuerySet":
        """Aktuell am Rad."""
        return self.filter(status=AssemblyStatus.ACTIVE)

    def parked(self) -> "BikeAssemblyQuerySet":
        """Abgezogen, aber nicht ausgemustert — die Wechsel-Alternativen."""
        return self.filter(status=AssemblyStatus.PARKED)

    def retired(self) -> "BikeAssemblyQuerySet":
        """Verkauft/entsorgt — nur noch Historie."""
        return self.filter(status=AssemblyStatus.RETIRED)


class BikeAssembly(models.Model):
    """
    Instanz einer Baugruppe (ComponentGroup) an einem konkreten Bike, z.B.
    "Laufrad vorne" oder – vom User umbenannt – "Sommer-LRS". Bündelt die
    zugehörigen `ComponentSlot`s. Pro `(bike, group)` darf höchstens eine
    Instanz `status=ACTIVE` sein — erzwungen durch einen partiellen
    Unique-Constraint in der DB, nicht nur durch `clean()`; die übrigen bleiben
    als Wechsel-Historie erhalten. Gleichzeitige Baugruppen unterschiedlicher
    `group` sind natürlich erlaubt.
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
    retired_at = models.DateField(
        null=True,
        blank=True,
        help_text="Tag des Ausmusterns. Nur bei status=retired gesetzt.",
    )
    status = models.CharField(
        max_length=10,
        choices=AssemblyStatus.choices,
        default=AssemblyStatus.ACTIVE,
        db_index=True,
    )
    # Was beim Aufziehen dieser Baugruppe mitwechselt. Zwei Arten, weil es zwei
    # verschiedene Dinge sind: ein zweites Laufrad wird als Ganzes aufgezogen, von der
    # Bremse wechselt dagegen nur der Belag — die Bremse selbst bleibt am Rad.
    linked_assemblies = models.ManyToManyField(
        "self",
        blank=True,
        symmetrical=True,
        help_text="Baugruppen, die zusammengehören (Vorder- und Hinterrad eines Satzes).",
    )
    linked_components = models.ManyToManyField(
        "Component",
        blank=True,
        related_name="linked_to_assemblies",
        help_text=(
            "Einzelne Teile aus anderen Bereichen, die beim Aufziehen dieser "
            "Baugruppe mit montiert werden (Bremsbelaege zum Carbon-Laufrad)."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = BikeAssemblyQuerySet.as_manager()

    class Meta:
        ordering = ["group__sort_order", "group__name", "-created_at"]
        verbose_name = "Baugruppe (Bike-Instanz)"
        verbose_name_plural = "Baugruppen (Bike-Instanzen)"
        constraints = [
            # Die eigentliche Garantie: höchstens eine aufgezogene Instanz je
            # (bike, group). `clean()` liefert nur die freundlichere Meldung und
            # kann eine Race zwischen zwei parallelen Requests nicht verhindern.
            models.UniqueConstraint(
                fields=["bike", "group"],
                condition=models.Q(status=AssemblyStatus.ACTIVE),
                name="uniq_active_assembly_per_bike_group",
            ),
        ]

    if TYPE_CHECKING:
        id: int
        bike_id: int
        group_id: int
        slots: RelatedManager["ComponentSlot"]
        intervals: RelatedManager["MaintenanceInterval"]
        periods: RelatedManager["AssemblyUsagePeriod"]

    def __str__(self):
        return f"{self.bike.name} — {self.display_name}"

    @property
    def display_name(self) -> str:
        return self.name or self.group.name

    @property
    def is_active(self) -> bool:
        """Aktuell am Rad."""
        return self.status == AssemblyStatus.ACTIVE

    @property
    def is_parked(self) -> bool:
        """
        Abgezogen, aber nicht entsorgt — z.B. der Sommer-LRS im Keller. Die
        Komponenten bleiben `is_mounted=True` (sie sitzen ja weiter auf dem
        Laufrad), nur die Baugruppe ist gerade nicht am Rad.
        """
        return self.status == AssemblyStatus.PARKED

    @property
    def is_retired(self) -> bool:
        """Verkauft/entsorgt — die Komponenten wurden ausgebaut."""
        return self.status == AssemblyStatus.RETIRED

    def open_period(self) -> "AssemblyUsagePeriod | None":
        """Die laufende (noch nicht beendete) Nutzungsperiode, falls montiert."""
        for period in self.periods.all():
            if period.ended_at is None:
                return period
        return None

    def sync_usage_period(self, at: datetime | None = None) -> None:
        """
        Hält die Invariante: offene Periode genau dann, wenn die Baugruppe am Rad ist.
        Ohne `at` beginnt eine neue Periode beim Einbau, ein Ende liegt bei jetzt.
        """
        open_period = self.periods.filter(ended_at__isnull=True).first()
        if self.is_active and open_period is None:
            AssemblyUsagePeriod.objects.create(
                assembly=self, started_at=at or moment_for(self.installed_at)
            )
        elif not self.is_active and open_period is not None:
            open_period.close(at or ride_clock_now())

    def pull_first_period_back(self, moment: datetime) -> None:
        """
        Zieht den Beginn der ersten Periode zurück, wenn ein Teil schon früher am Rad
        war. Davor liegt nichts mehr, Überlappungen sind also ausgeschlossen. Nach
        vorne geschoben wird nie: ein später eingebautes Einzelteil verkürzt die Zeit
        der Baugruppe am Rad nicht.
        """
        first = self.periods.order_by("started_at").first()
        if first is not None and moment < first.started_at:
            first.started_at = moment
            first.save(update_fields=["started_at"])

    def clean(self):
        # Doppelt zum DB-Constraint (siehe Meta), aber mit verständlicher
        # Meldung statt IntegrityError — die API gibt sie als 400 weiter.
        if self.is_active:
            qs = (
                BikeAssembly.objects.filter(bike=self.bike, group=self.group)
                .active()
                .exclude(pk=self.pk)
            )
            if qs.exists():
                raise ValidationError(
                    f"Für '{self.group.name}' an diesem Bike ist bereits eine "
                    "Baugruppe aktiv. Bitte zuerst die aktuelle deaktivieren."
                )

    def save(self, *args, usage_at: datetime | None = None, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)
        self.sync_usage_period(usage_at)

    def compute_km(self, ledger: RideLedger | None = None) -> float:
        """Gefahrene km MIT dieser Baugruppe — Summe über ihre Zeiten am Rad."""
        return assembly_km(self, ledger or RideLedger.for_bike(self.bike_id))

    def worst_status(self, ledger: RideLedger | None = None) -> str:
        """Schlechtester Status über montierte Komponenten + zugehörige Intervalle."""
        from .api.serializers import compute_wear

        ledger = ledger or RideLedger.for_bike(self.bike_id)
        statuses: list[str] = []
        for slot in self.slots.all():
            comp = slot.mounted_component
            if comp is None:
                statuses.append(WarnLevel.UNKNOWN)
                continue
            statuses.append(compute_wear(comp, ledger)["warn_status_overall"])
        for interval in self.intervals.all():
            statuses.append(interval.status(ledger))

        return worst_of(statuses)


class AssemblyUsagePeriod(models.Model):
    """
    Ein Zeitraum, in dem eine `BikeAssembly` tatsächlich am Rad war — Grundlage für
    `assembly_km` und dafür, dass ein Teil nicht in eine Zeit zurückdatiert werden
    kann, in der die Baugruppe im Keller lag. Die km der einzelnen Teile hängen an
    deren eigenen Zeiträumen (`ComponentUsagePeriod`).

    Offene Periode (`ended_at is None`) = aktuell montiert. Es gibt höchstens
    eine offene Periode je Baugruppe, und nur bei `status=active`. Alle Zeitpunkte
    liegen auf der Uhr der Fahrten (siehe `api/usage.py`).
    """

    assembly = models.ForeignKey(
        BikeAssembly,
        on_delete=models.CASCADE,
        related_name="periods",
    )
    started_at = models.DateTimeField(help_text="Zeitpunkt der Montage.")
    ended_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Zeitpunkt des Abziehens. None = aktuell montiert.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["started_at", "id"]
        verbose_name = "Baugruppen-Nutzungszeitraum"
        verbose_name_plural = "Baugruppen-Nutzungszeiträume"
        indexes = [models.Index(fields=["assembly", "ended_at"])]
        constraints = [
            models.UniqueConstraint(
                fields=["assembly"],
                condition=models.Q(ended_at__isnull=True),
                name="uniq_open_period_per_assembly",
            ),
        ]

    if TYPE_CHECKING:
        id: int

    def __str__(self):
        bis = self.ended_at.date().isoformat() if self.ended_at else "heute"
        return f"{self.assembly.display_name}: {self.started_at.date()} – {bis}"

    @property
    def is_open(self) -> bool:
        return self.ended_at is None

    def close(self, at: datetime) -> None:
        """Beenden. Ein leerer Zeitraum (gleich wieder abgezogen) bleibt nicht stehen."""
        if at <= self.started_at:
            self.delete()
            return
        self.ended_at = at
        self.save(update_fields=["ended_at"])


class ComponentSlotQuerySet(models.QuerySet):
    def on_bike(self, bike) -> "ComponentSlotQuerySet":
        """
        Nur Slots, die aktuell wirklich am Rad sind: die einer aktiven Baugruppe
        plus die noch ungruppierten Alt-Slots. Blendet damit geparkte Baugruppen
        (z.B. der Winter-LRS im Keller) aus Bike-Ampel, Diagramm,
        Zustandsbericht und Warn-E-Mails aus.
        """
        return self.filter(bike=bike).filter(
            models.Q(assembly__isnull=True)
            | models.Q(assembly__status=AssemblyStatus.ACTIVE)
        )


class ComponentSlot(models.Model):
    """
    Ein logischer Steckplatz am Fahrrad, z.B. 'Kette', 'Reifen vorne'.
    Mehrere Component-Instanzen können einem Slot zugeordnet sein (der Sommer- und
    der Winterreifen desselben Laufrads), aber immer nur eine ist montiert.

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

    objects = ComponentSlotQuerySet.as_manager()

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
        bike_id: int
        assembly_id: int | None
        template_id: int
        components: RelatedManager["Component"]

    def __str__(self):
        return f"{self.bike.name} — {self.display_name}"

    @property
    def display_name(self) -> str:
        return self.custom_name or self.template.name

    @property
    def mounted_component(self):
        """
        Bewusst `components.all()` statt `.filter(is_mounted=True)`: ein
        `.filter()` auf einem prefetchten Related-Manager umgeht den
        Prefetch-Cache und setzt je Slot eine eigene Query ab. Da je Slot
        höchstens ein Teil montiert sein darf (siehe `Component.clean()`), ist
        das Ergebnis identisch.
        """
        for component in self.components.all():
            if component.is_mounted:
                return component
        return None


class ComponentStatus(models.TextChoices):
    """
    Die drei Zustände eines Teils — analog zu `AssemblyStatus` ein Feld statt einer
    Kodierung über mehrere.

    Vorher gab es nur "montiert ja/nein", und `retired_at` wurde bei jedem Ausbau
    gesetzt. Damit war nicht unterscheidbar, ob ein abgenommener Reifen im Regal
    liegt und noch gut ist oder längst im Müll.
    """

    MOUNTED = "mounted", "Montiert"
    SPARE = "spare", "Ersatz"
    RETIRED = "retired", "Ausgemustert"


class ComponentQuerySet(models.QuerySet):
    def mounted(self) -> "ComponentQuerySet":
        return self.filter(status=ComponentStatus.MOUNTED)

    def spare(self) -> "ComponentQuerySet":
        return self.filter(status=ComponentStatus.SPARE)

    def retired(self) -> "ComponentQuerySet":
        return self.filter(status=ComponentStatus.RETIRED)


class Component(models.Model):
    """
    Eine konkrete physische Komponente, die einem Slot zugeordnet ist.
    Mehrere können einem Slot gehören (Ersatzreifen, alte Kette etc.),
    aber nur eine davon ist montiert (Partial-Unique-Constraint in der DB).

    Ihre Kilometer sind nirgends gespeichert: sie ergeben sich aus den Fahrten in
    ihren Nutzungszeiträumen (`periods`, siehe `api/usage.py`). Montiert auf einer
    geparkten Baugruppe heißt: das Teil sitzt auf dem Laufrad, aber das Laufrad ist
    nicht am Rad — dann läuft kein Zeitraum.
    """

    slot = models.ForeignKey(
        ComponentSlot,
        on_delete=models.CASCADE,
        related_name="components",
    )
    brand = models.CharField(max_length=100, blank=True)
    model_name = models.CharField(max_length=100, blank=True)

    status = models.CharField(
        max_length=10,
        choices=ComponentStatus.choices,
        default=ComponentStatus.MOUNTED,
        db_index=True,
    )
    installed_at = models.DateField(
        null=True,
        blank=True,
        help_text=(
            "Tag des ersten Einbaus. Bleibt bei späteren Wechseln stehen, denn daran "
            "hängt die Tage-Achse: Gummi altert auch, während das Teil im Regal liegt."
        ),
    )
    retired_at = models.DateField(
        null=True,
        blank=True,
        help_text="Tag des Ausmusterns. Nur bei status=retired gesetzt.",
    )
    prior_km = models.FloatField(
        null=True,
        blank=True,
        help_text=(
            "Km, die das Teil schon hatte, bevor es hier erfasst wurde (gebraucht "
            "gekauft, vor der App gefahren). Wird auf die gefahrenen km addiert."
        ),
    )
    prior_weather_km = models.FloatField(
        null=True,
        blank=True,
        help_text="Analog zu prior_km für die wetter-gewichtete Achse.",
    )
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

    objects = ComponentQuerySet.as_manager()

    class Meta:
        ordering = ["-installed_at", "-id"]
        constraints = [
            # Die eigentliche Garantie; `clean()` liefert nur die freundlichere Meldung.
            models.UniqueConstraint(
                fields=["slot"],
                condition=models.Q(status="mounted"),
                name="uniq_mounted_component_per_slot",
            ),
        ]

    if TYPE_CHECKING:
        id: int
        slot_id: int
        checks: RelatedManager["ComponentCheck"]
        periods: RelatedManager["ComponentUsagePeriod"]
        linked_to_assemblies: RelatedManager["BikeAssembly"]

        def get_status_display(self) -> str: ...

    def __str__(self):
        return f"{self.label} [{self.get_status_display()}] @ {self.slot}"

    @property
    def label(self) -> str:
        """Was der Nutzer als Namen des Teils sieht: Marke + Modell, sonst der Slot."""
        return f"{self.brand} {self.model_name}".strip() or self.slot.display_name

    @property
    def is_mounted(self) -> bool:
        return self.status == ComponentStatus.MOUNTED

    @property
    def is_spare(self) -> bool:
        return self.status == ComponentStatus.SPARE

    @property
    def is_retired(self) -> bool:
        return self.status == ComponentStatus.RETIRED

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

    def collects_km(self) -> bool:
        """Sammelt das Teil gerade km? Montiert — und die Baugruppe ist am Rad."""
        if not self.is_mounted:
            return False
        assembly = self.slot.assembly
        return assembly is None or assembly.is_active

    def sync_usage_period(self, at: datetime | None = None) -> None:
        """
        Hält die Invariante: offener Zeitraum genau dann, wenn das Teil gerade km
        sammelt. Jeder Zustandswechsel läuft hier durch, deshalb können km und Status
        nicht auseinanderlaufen. Ohne `at` beginnt ein erster Zeitraum beim Einbau,
        alles andere bei jetzt.
        """
        open_period = self.periods.filter(ended_at__isnull=True).first()
        collects = self.collects_km()
        if collects and open_period is None:
            if at is None:
                first_time = not self.periods.exists()
                at = moment_for(self.installed_at) if first_time else ride_clock_now()
            ComponentUsagePeriod.objects.create(component=self, started_at=at)
        elif not collects and open_period is not None:
            open_period.close(at or ride_clock_now())

    def clean(self):
        """Stellt sicher dass pro Slot maximal eine Komponente montiert ist."""
        if self.is_mounted:
            qs = Component.objects.mounted().filter(slot=self.slot).exclude(pk=self.pk)
            if qs.exists():
                raise ValidationError(
                    f"Im Slot '{self.slot.display_name}' ist bereits eine "
                    "Komponente montiert. Bitte zuerst die aktuelle ausbauen."
                )

    def save(self, *args, usage_at: datetime | None = None, **kwargs):
        if self.is_mounted and self.installed_at is None:
            self.installed_at = (usage_at or ride_clock_now()).date()
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = [*kwargs["update_fields"], "installed_at"]
        self.full_clean()
        super().save(*args, **kwargs)
        self.sync_usage_period(usage_at)


class ComponentUsagePeriod(models.Model):
    """
    Ein Zeitraum, in dem dieses Teil tatsächlich gefahren wurde. Aus diesen Zeiträumen
    und den Fahrten ergeben sich seine Kilometer (siehe `api/usage.py`).

    Beim Parken einer Baugruppe enden die Zeiträume ihrer Teile, beim Aufziehen
    beginnen neue — die Zeiträume stehen also für sich und müssen nicht mit denen der
    Baugruppe verschnitten werden. Dadurch kann ein Teil auch auf ein anderes Laufrad
    umziehen, ohne seine Vergangenheit zu verlieren.
    """

    component = models.ForeignKey(
        Component,
        on_delete=models.CASCADE,
        related_name="periods",
    )
    started_at = models.DateTimeField(help_text="Ab hier zählen die Fahrten.")
    ended_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Fahrten ab hier zählen nicht mehr. None = läuft noch.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["started_at", "id"]
        verbose_name = "Komponenten-Nutzungszeitraum"
        verbose_name_plural = "Komponenten-Nutzungszeiträume"
        indexes = [models.Index(fields=["component", "ended_at"])]
        constraints = [
            models.UniqueConstraint(
                fields=["component"],
                condition=models.Q(ended_at__isnull=True),
                name="uniq_open_period_per_component",
            ),
        ]

    if TYPE_CHECKING:
        id: int

    def __str__(self):
        bis = self.ended_at.date().isoformat() if self.ended_at else "heute"
        return f"{self.component.label}: {self.started_at.date()} – {bis}"

    def close(self, at: datetime) -> None:
        """Beenden. Ein leerer Zeitraum (gleich wieder abgenommen) bleibt nicht stehen."""
        if at <= self.started_at:
            self.delete()
            return
        self.ended_at = at
        self.save(update_fields=["ended_at"])


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
    baseline_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "Ab hier zählen die km für den Warn-Status neu (Uhr der Fahrten). Leer = "
            "ab dem Morgen von checked_at."
        ),
    )
    checked_at_weather_wear_km = models.FloatField(
        null=True,
        blank=True,
        help_text=(
            "component.weather_wear_km zum Zeitpunkt der Prüfung — Baseline, damit "
            "der wetterbereinigte Warn-Status nach einer Freigabe ebenfalls zurückgesetzt "
            "wird (analog zu baseline_at für die km-Achse)."
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
        help_text="Erneut warnen nach X weiteren km ab der Prüfung",
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

    def baseline_moment(self) -> datetime:
        return self.baseline_at or start_of_day(self.checked_at)


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
    km_baseline_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "Ab hier zählen die km seit der letzten Erledigung (Uhr der Fahrten). "
            "Leer = ab dem Morgen von last_done_at."
        ),
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
        bike_id: int
        assembly_id: int | None
        logs: RelatedManager["MaintenanceLog"]

    def __str__(self):
        return f"{self.bike.name} — {self.label}"

    @property
    def is_on_bike(self) -> bool:
        """Intervalle einer geparkten/ausgemusterten Baugruppe betreffen das Rad nicht."""
        return self.assembly_id is None or self.assembly.is_active

    def km_baseline(self) -> datetime | None:
        """Ab wann die km zählen. None = noch nie erledigt, also alle Fahrten."""
        if self.km_baseline_at is not None:
            return self.km_baseline_at
        return start_of_day(self.last_done_at) if self.last_done_at else None

    def km_since_done(self, ledger: RideLedger | None = None) -> float:
        return interval_km_since_done(self, ledger or RideLedger.for_bike(self.bike_id))

    def usage_ratio(
        self, ledger: RideLedger | None = None, as_of: date | None = None
    ) -> float | None:
        """Verbrauchter Anteil des Intervalls, schlechteste Achse. None = keine Grenze."""
        as_of = as_of or date.today()
        ratios: list[float] = []
        if self.interval_km:
            ratios.append(self.km_since_done(ledger) / self.interval_km)
        if self.interval_days and self.last_done_at:
            ratios.append((as_of - self.last_done_at).days / self.interval_days)
        return max(ratios) if ratios else None

    def status(
        self, ledger: RideLedger | None = None, as_of: date | None = None
    ) -> str:
        """
        Ampel wie `compute_wear`: schlechtester Wert aus km- und Tage-Achse.
        `as_of` projiziert nur die Tage-Achse (Default: heute) — analog zur
        Fahrt-Vorhersage in app_notifications.
        """
        return warn_status_from_ratio(self.usage_ratio(ledger, as_of))


class MaintenanceLog(models.Model):
    """Ein Eintrag "erledigt am" für ein MaintenanceInterval (Historie)."""

    interval = models.ForeignKey(
        MaintenanceInterval,
        on_delete=models.CASCADE,
        related_name="logs",
    )
    done_at = models.DateField(default=date.today)
    # Der Stand des Intervalls vor diesem Eintrag — damit "Rückgängig" ihn exakt
    # wiederherstellen kann, auch wenn es davor noch gar keinen Eintrag gab.
    previous_done_at = models.DateField(null=True, blank=True)
    previous_baseline_at = models.DateTimeField(null=True, blank=True)
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
