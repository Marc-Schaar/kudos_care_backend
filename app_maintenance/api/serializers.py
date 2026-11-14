from datetime import date
from typing import cast

from rest_framework import serializers

from app_maintenance.models import (
    Bike,
    BikeAssembly,
    Component,
    ComponentCheck,
    ComponentGroup,
    ComponentSlot,
    ComponentStatus,
    ComponentTemplate,
    ComponentUsagePeriod,
    MaintenanceInterval,
    MaintenanceIntervalKind,
    MaintenanceKind,
    MaintenanceLog,
    WarnLevel,
    warn_status_from_ratio,
    worst_of,
)

from .usage import (
    RideLedger,
    assembly_windows,
    component_km,
    moment_for,
    ride_clock_now,
)


def bike_total_km(context: dict, bike: Bike) -> float | None:
    """
    Gesamtkilometer eines Bikes — einmal je Request bestimmt, nicht je Slot.

    `Bike.total_distance_km` ist ein Property mit eigener Aggregat-Query. Ohne
    diesen Cache liest jeder Slot und jedes Intervall es erneut; in der
    Bike-Liste waren das gemessen 18 identische `SUM`-Queries pro Bike.

    Wird je Bike-PK gecacht, damit ein Context über mehrere Bikes hinweg nicht die
    Zahl des falschen Bikes zurückgibt.
    """
    key = f"_bike_km_{bike.pk}"
    if key not in context:
        context[key] = bike.total_distance_km
    return context[key]


def ride_ledger(context: dict, bike_id: int) -> RideLedger:
    """
    Die Fahrten eines Bikes — einmal je Request und Bike geladen, nicht je Teil. Die
    Anzahl der Teile darf die Anzahl der Queries nicht beeinflussen (siehe
    test_query_counts.py).
    """
    key = f"_ride_ledger_{bike_id}"
    if key not in context:
        context[key] = RideLedger.for_bike(bike_id)
    return context[key]


def slots_on_bike(bike: Bike) -> list[ComponentSlot]:
    """
    Die Slots, die aktuell wirklich am Rad sind: die aktiver Baugruppen plus die
    ungruppierten Alt-Slots. Geparkte Baugruppen (z.B. der Winter-LRS im Keller)
    dürfen weder in die Bike-Ampel noch ins Diagramm einfließen.

    Filtert bewusst in Python statt per Query, damit das `prefetch_related` der
    Views (`slots__assembly`) greift und keine N+1 entsteht — das DB-Pendant ist
    `ComponentSlot.objects.on_bike()`.
    """
    return [
        slot
        for slot in bike.slots.all()
        if slot.assembly_id is None or slot.assembly.is_active
    ]


def compute_wear(
    component: Component, ledger: RideLedger | None = None, as_of: date | None = None
) -> dict:
    """
    Berechnet Verschleiß und Warn-Status einer Komponente. Gibt ein Dict zurück das
    direkt im Serializer verwendet wird.

    `wear_km`/`wear_days` sind informative Totalwerte seit dem ersten Einbau. Der
    Warn-Status wird dagegen — falls eine Prüfung (ComponentCheck) vorliegt — ab dem
    Zeitpunkt der letzten Prüfung neu berechnet ("Freigeben"), mit dem dabei
    angegebenen Snooze-Intervall (falls keins angegeben wurde, gilt ab der Prüfung
    wieder die normale empfohlene/individuelle Lebensdauer).

    Die km kommen aus den Fahrten in den Nutzungszeiträumen des Teils (siehe
    api/usage.py) — Zeiten im Regal oder auf einem abgezogenen Laufrad zählen nicht.
    Die Tage-Achse zählt bewusst durchgehend weiter (Gummi/Dichtmilch altern auch im
    Keller) und bleibt erst beim Ausmustern stehen.

    `wear_ratio` ist der verbrauchte Anteil der Lebensdauer auf der schlechtesten
    Achse — damit lassen sich Teile untereinander und gegen Intervalle sortieren.

    `as_of` erlaubt eine Projektion auf ein zukünftiges Datum (siehe
    app_notifications — "voraussichtlich unsafe bei nächster Fahrt"): nur die
    Tage-Achse wird dadurch wirklich projiziert, die km bleiben beim heutigen Stand,
    da zukünftige Distanz nicht bekannt ist. Default (None) = heute.
    """
    as_of = as_of or date.today()
    if component.is_retired and component.retired_at:
        as_of = min(as_of, component.retired_at)
    ledger = ledger or RideLedger.for_bike(component.slot.bike_id)
    warn_km = component.effective_warn_km
    warn_days = component.effective_warn_days

    wear_km = round(component_km(component, ledger) + (component.prior_km or 0.0), 1)
    # max(): direkt nach Mitternacht kann der Server (UTC) noch beim Vortag sein.
    wear_days = (
        max((as_of - component.installed_at).days, 0)
        if component.installed_at
        else None
    )

    # ── Status-Baseline: letzte Prüfung falls vorhanden, sonst Einbau ────────
    latest_check = component.checks.first()
    if latest_check is not None:
        km_for_status = component_km(
            component, ledger, since=latest_check.baseline_moment()
        )
        days_for_status = max((as_of - latest_check.checked_at).days, 0)
        threshold_km = latest_check.snooze_km or warn_km
        threshold_days = latest_check.snooze_days or warn_days
    else:
        km_for_status = wear_km
        days_for_status = wear_days
        threshold_km = warn_km
        threshold_days = warn_days

    def axis_status(
        value: float | None, threshold: float | None
    ) -> tuple[str, float | None]:
        if value is None:
            return WarnLevel.UNKNOWN, None
        if not threshold:
            return WarnLevel.OK, None
        ratio = value / threshold
        return warn_status_from_ratio(ratio), ratio

    status_km, ratio_km = axis_status(km_for_status, threshold_km)
    status_days, ratio_days = axis_status(days_for_status, threshold_days)

    # ── Wetter-gewichteter km-Verschleiß ──────────────────────────────────────
    # component.weather_wear_km wird asynchron von WeatherWearService befüllt
    # (siehe app_maintenance/api/services.py), nicht hier live berechnet — ist
    # aber immer der volle Verlauf seit Einbau. Liegt eine Prüfung vor, wird
    # daher analog zur km-/Tage-Achse ab der bei der Prüfung gespeicherten
    # Baseline (checked_at_weather_wear_km) gerechnet, sonst bleibt eine
    # Freigabe für diese Achse wirkungslos und die Komponente erscheint trotz
    # "Freigeben" weiter als überfällig.
    weather_km_for_status = None
    if component.weather_wear_km is not None:
        baseline = latest_check.checked_at_weather_wear_km if latest_check else None
        weather_km_for_status = component.weather_wear_km - (baseline or 0.0)
    status_weather, ratio_weather = axis_status(weather_km_for_status, warn_km)

    ratios = [r for r in (ratio_km, ratio_days, ratio_weather) if r is not None]

    return {
        "wear_km": wear_km,
        "wear_days": wear_days,
        "wear_ratio": round(max(ratios), 3) if ratios else None,
        "warn_status_km": status_km,
        "warn_status_days": status_days,
        "warn_status_weather_km": status_weather,
        # Gesamt-Status = schlechtester Einzelwert
        "warn_status_overall": worst_of([status_km, status_days, status_weather]),
    }


def bike_warn_status(context: dict, bike: Bike) -> str:
    """
    Die Ampel eines Bikes: schlechtester Wert über alles, was gerade wirklich am Rad
    ist. Teile und Intervalle einer geparkten oder ausgemusterten Baugruppe zählen
    nicht — die überfällige Dichtmilch eines längst entsorgten Laufrads färbte das Rad
    sonst dauerhaft rot.
    """
    ledger = ride_ledger(context, bike.pk)
    statuses: list[str] = []
    for slot in slots_on_bike(bike):
        comp = slot.mounted_component
        if comp is not None:
            statuses.append(compute_wear(comp, ledger)["warn_status_overall"])
    for interval in bike.intervals.all():
        if interval.is_on_bike:
            statuses.append(interval.status(ledger))
    return worst_of(statuses)


class ComponentTemplateSerializer(serializers.ModelSerializer[ComponentTemplate]):
    category_display = serializers.CharField(
        source="get_category_display", read_only=True
    )
    group_name = serializers.CharField(
        source="group.name", read_only=True, default=None
    )

    class Meta:
        model = ComponentTemplate
        fields = [
            "id",
            "name",
            # Vorne/hinten als Feld — das Diagramm liest das statt den
            # Anzeigenamen zu durchsuchen (siehe models.py::MountPosition).
            "position",
            "category",
            "category_display",
            "applicable_bike_types",
            "warn_km",
            "warn_hours",
            "warn_days",
            "is_system",
            "supports_condition_estimate",
            "maintenance_kind",
            "default_in_group",
            "follows_wheel",
            "notes",
            "group",
            "group_name",
        ]
        read_only_fields = ["is_system", "follows_wheel"]


class ComponentCheckSerializer(serializers.ModelSerializer[ComponentCheck]):
    """Kompakte, read-only Zusammenfassung der letzten Prüfung."""

    class Meta:
        model = ComponentCheck
        fields = [
            "id",
            "checked_at",
            "checked_at_weather_wear_km",
            "condition_pct",
            "snooze_km",
            "snooze_days",
            "note",
        ]


class ComponentUsagePeriodSerializer(serializers.ModelSerializer[ComponentUsagePeriod]):
    """Ein Einsatz des Teils — das, was der Verlauf zeigt."""

    km = serializers.SerializerMethodField()

    class Meta:
        model = ComponentUsagePeriod
        fields = ["id", "started_at", "ended_at", "km"]

    def get_km(self, obj: ComponentUsagePeriod) -> float:
        ledger = self.context.get("ledger")
        if ledger is None:
            return 0.0
        return round(ledger.km_between(obj.started_at, obj.ended_at), 1)


class ComponentSerializer(serializers.ModelSerializer[Component]):
    """
    Ein Teil, egal ob montiert, im Regal oder ausgemustert. `status` ist hier bewusst
    read-only: ein Wechsel muss über mount/unmount/retire/restore laufen, sonst bliebe
    der Nutzungszeitraum offen und das Teil sammelte weiter km.
    """

    label = serializers.CharField(read_only=True)
    is_mounted = serializers.BooleanField(read_only=True)
    effective_warn_km = serializers.FloatField(read_only=True)
    effective_warn_days = serializers.IntegerField(read_only=True)
    condition_pct = serializers.SerializerMethodField()
    wear_km = serializers.SerializerMethodField()
    wear_days = serializers.SerializerMethodField()
    wear_ratio = serializers.SerializerMethodField()
    warn_status_km = serializers.SerializerMethodField()
    warn_status_days = serializers.SerializerMethodField()
    warn_status_weather_km = serializers.SerializerMethodField()
    warn_status_overall = serializers.SerializerMethodField()
    last_check = serializers.SerializerMethodField()
    periods = serializers.SerializerMethodField()

    class Meta:
        model = Component
        fields = [
            "id",
            "slot",
            "label",
            "brand",
            "model_name",
            "status",
            "is_mounted",
            "installed_at",
            "retired_at",
            "prior_km",
            "notes",
            "custom_warn_km",
            "custom_warn_days",
            "effective_warn_km",
            "effective_warn_days",
            "created_at",
            "updated_at",
            "wear_km",
            "wear_days",
            "wear_ratio",
            "warn_status_km",
            "warn_status_days",
            "weather_wear_km",
            "weather_wear_computed_at",
            "weather_wear_ride_count",
            "warn_status_weather_km",
            "warn_status_overall",
            "condition_pct",
            "last_check",
            "periods",
        ]
        read_only_fields = [
            "slot",
            "status",
            "retired_at",
            "created_at",
            "updated_at",
            "weather_wear_km",
            "weather_wear_computed_at",
            "weather_wear_ride_count",
        ]

    def validate_installed_at(self, value):
        if value is not None and value > ride_clock_now().date():
            raise serializers.ValidationError(
                "Das Einbaudatum darf nicht in der Zukunft liegen."
            )
        return value

    def _ledger(self, obj: Component) -> RideLedger:
        return ride_ledger(cast(dict, self.context), obj.slot.bike_id)

    def get_condition_pct(self, obj: Component) -> int | None:
        latest_check = obj.checks.first()
        return latest_check.condition_pct if latest_check else None

    def get_last_check(self, obj):
        latest_check = obj.checks.first()
        if latest_check is None:
            return None
        return ComponentCheckSerializer(latest_check).data

    def get_periods(self, obj: Component):
        return ComponentUsagePeriodSerializer(
            obj.periods.all(), many=True, context={"ledger": self._ledger(obj)}
        ).data

    def _get_wear(self, obj: Component) -> dict:
        """Wear-Dict einmal berechnen und im Serializer-Context cachen."""
        context = cast(dict, self.context)
        cache_key = f"_wear_{obj.pk}"
        if cache_key not in context:
            context[cache_key] = compute_wear(obj, self._ledger(obj))
        return context[cache_key]

    def get_wear_km(self, obj):
        return self._get_wear(obj)["wear_km"]

    def get_wear_days(self, obj):
        return self._get_wear(obj)["wear_days"]

    def get_wear_ratio(self, obj):
        return self._get_wear(obj)["wear_ratio"]

    def get_warn_status_km(self, obj):
        return self._get_wear(obj)["warn_status_km"]

    def get_warn_status_days(self, obj):
        return self._get_wear(obj)["warn_status_days"]

    def get_warn_status_weather_km(self, obj):
        return self._get_wear(obj)["warn_status_weather_km"]

    def get_warn_status_overall(self, obj):
        return self._get_wear(obj)["warn_status_overall"]


class ComponentCreateSerializer(serializers.Serializer):
    """Body für POST /slots/{id}/components/."""

    brand = serializers.CharField(
        max_length=100, required=False, allow_blank=True, default=""
    )
    model_name = serializers.CharField(
        max_length=100, required=False, allow_blank=True, default=""
    )
    notes = serializers.CharField(required=False, allow_blank=True, default="")
    custom_warn_km = serializers.FloatField(
        required=False, allow_null=True, min_value=0
    )
    custom_warn_days = serializers.IntegerField(
        required=False, allow_null=True, min_value=0
    )
    prior_km = serializers.FloatField(required=False, allow_null=True, min_value=0)
    # Gleich aufziehen (und das bisherige Teil abnehmen) oder nur ins Regal legen.
    mount = serializers.BooleanField(required=False, default=True)
    # Seit wann das Teil dran ist; ohne Angabe ab jetzt. Nur mit `mount` relevant.
    installed_at = serializers.DateField(required=False, allow_null=True)


class ComponentActionSerializer(serializers.Serializer):
    """Body für mount/unmount/retire: optional, an welchem Tag das passiert ist."""

    on = serializers.DateField(required=False, allow_null=True)


class ComponentMoveSerializer(ComponentActionSerializer):
    """Body für POST /components/{id}/move/."""

    slot_id = serializers.IntegerField()
    mount = serializers.BooleanField(required=False, default=True)


class ComponentCheckCreateSerializer(serializers.Serializer):
    """
    Validiert den Body für POST /components/{id}/check/ ("Prüfen/Freigeben").
    Alle Felder sind optional — ohne Angaben wird die Komponente einfach ab
    heute wieder für den normalen Lebenszyklus freigegeben.
    """

    condition_pct = serializers.IntegerField(
        required=False, allow_null=True, min_value=0, max_value=100
    )
    snooze_km = serializers.FloatField(required=False, allow_null=True, min_value=0)
    snooze_days = serializers.IntegerField(required=False, allow_null=True, min_value=0)
    note = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        component = self.context["component"]
        if (
            attrs.get("condition_pct") is not None
            and not component.slot.template.supports_condition_estimate
        ):
            raise serializers.ValidationError(
                {
                    "condition_pct": "Für diesen Komponententyp ist keine Zustandsschätzung möglich."
                }
            )
        return attrs


class ComponentSlotSerializer(serializers.ModelSerializer[ComponentSlot]):
    """
    Ein Platz am Rad mit allem, was dort je saß: das montierte Teil, die Ersatzteile im
    Regal und die ausgemusterten. Die Art des Platzes (`template`) steht fest und ist
    nur beim Anlegen wählbar.
    """

    display_name = serializers.CharField(read_only=True)
    category = serializers.CharField(source="template.category", read_only=True)
    category_display = serializers.CharField(
        source="template.get_category_display", read_only=True
    )
    template_detail = ComponentTemplateSerializer(source="template", read_only=True)
    warn_status = serializers.SerializerMethodField()
    mounted_component = serializers.SerializerMethodField()
    spare_components = serializers.SerializerMethodField()
    retired_components = serializers.SerializerMethodField()

    class Meta:
        model = ComponentSlot
        fields = [
            "id",
            "bike",
            "assembly",
            "template",
            "template_detail",
            "custom_name",
            "display_name",
            "category",
            "category_display",
            "warn_status",
            "mounted_component",
            "spare_components",
            "retired_components",
        ]
        read_only_fields = ["bike", "assembly", "template"]

    def _parts(self, obj: ComponentSlot, status: str):
        parts = [c for c in obj.components.all() if c.status == status]
        return ComponentSerializer(parts, many=True, context=self.context).data

    def get_warn_status(self, obj: ComponentSlot) -> str:
        comp = obj.mounted_component
        if comp is None:
            return WarnLevel.UNKNOWN
        return compute_wear(comp, ride_ledger(cast(dict, self.context), obj.bike_id))[
            "warn_status_overall"
        ]

    def get_mounted_component(self, obj: ComponentSlot):
        comp = obj.mounted_component
        if comp is None:
            return None
        return ComponentSerializer(comp, context=self.context).data

    def get_spare_components(self, obj: ComponentSlot):
        return self._parts(obj, ComponentStatus.SPARE)

    def get_retired_components(self, obj: ComponentSlot):
        return self._parts(obj, ComponentStatus.RETIRED)


class SpareComponentSerializer(serializers.ModelSerializer[Component]):
    """
    Ein Teil im Regal, mit seiner Herkunft — für die Übernahme auf einen anderen Platz
    (`POST components/{id}/move/`). `orphaned` heißt: seine Baugruppe ist ausgemustert,
    es braucht also einen neuen Platz, bevor es wieder aufgezogen werden kann.
    """

    template = serializers.IntegerField(source="slot.template_id", read_only=True)
    slot_name = serializers.CharField(source="slot.display_name", read_only=True)
    assembly = serializers.IntegerField(source="slot.assembly_id", read_only=True)
    assembly_name = serializers.SerializerMethodField()
    orphaned = serializers.SerializerMethodField()
    prior_wear_km = serializers.SerializerMethodField()

    class Meta:
        model = Component
        fields = [
            "id",
            "slot",
            "slot_name",
            "assembly",
            "assembly_name",
            "orphaned",
            "template",
            "label",
            "brand",
            "model_name",
            "status",
            "installed_at",
            "retired_at",
            "prior_wear_km",
        ]

    def get_assembly_name(self, obj: Component) -> str | None:
        assembly = obj.slot.assembly
        return assembly.display_name if assembly else None

    def get_orphaned(self, obj: Component) -> bool:
        assembly = obj.slot.assembly
        return assembly is not None and assembly.is_retired

    def get_prior_wear_km(self, obj: Component) -> float:
        """Bisher auf dem Teil gefahrene km ("dieses Teil hat schon X km")."""
        ledger = ride_ledger(cast(dict, self.context), obj.slot.bike_id)
        return round(component_km(obj, ledger) + (obj.prior_km or 0.0), 1)


class BikeSerializer(serializers.ModelSerializer[Bike]):
    """Ein Bike samt der Teile, die gerade am Rad sind."""

    bike_type_display = serializers.CharField(
        source="get_bike_type_display", read_only=True
    )
    total_distance_km = serializers.SerializerMethodField()
    slots = serializers.SerializerMethodField()

    warn_status = serializers.SerializerMethodField()

    class Meta:
        model = Bike
        fields = [
            "id",
            "strava_bike_id",
            "name",
            "bike_type",
            "bike_type_display",
            "retired",
            "total_distance_km",
            "warn_status",
            "slots",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["strava_bike_id", "created_at", "updated_at"]

    def get_total_distance_km(self, obj: Bike) -> float | None:
        return bike_total_km(cast(dict, self.context), obj)

    def get_slots(self, obj: Bike):
        return ComponentSlotSerializer(
            slots_on_bike(obj), many=True, context=self.context
        ).data

    def get_warn_status(self, obj: Bike) -> str:
        return bike_warn_status(cast(dict, self.context), obj)


class BikeListSerializer(serializers.ModelSerializer[Bike]):
    """Kompakte Variante für Listen ohne Slots."""

    bike_type_display = serializers.CharField(
        source="get_bike_type_display", read_only=True
    )
    total_distance_km = serializers.SerializerMethodField()
    warn_status = serializers.SerializerMethodField()

    class Meta:
        model = Bike
        fields = [
            "id",
            "strava_bike_id",
            "name",
            "bike_type",
            "bike_type_display",
            "retired",
            "total_distance_km",
            "warn_status",
        ]
        read_only_fields = ["strava_bike_id"]

    def get_total_distance_km(self, obj: Bike) -> float | None:
        return bike_total_km(cast(dict, self.context), obj)

    def get_warn_status(self, obj: Bike) -> str:
        return bike_warn_status(cast(dict, self.context), obj)


# ── Baugruppen (BikeAssembly) & Wartungs-Intervalle ───────────────────────────


class MaintenanceLogSerializer(serializers.ModelSerializer[MaintenanceLog]):
    class Meta:
        model = MaintenanceLog
        fields = ["id", "done_at", "note", "created_at"]


class MaintenanceIntervalSerializer(serializers.ModelSerializer[MaintenanceInterval]):
    """
    Intervall-Status ohne "Zustand". Die km kommen aus den Fahrten seit der letzten
    Erledigung (siehe api/usage.py), bei einer Baugruppe nur aus deren Zeiten am Rad.
    """

    status = serializers.SerializerMethodField()
    km_since = serializers.SerializerMethodField()
    days_since = serializers.SerializerMethodField()
    usage_ratio = serializers.SerializerMethodField()
    last_log = serializers.SerializerMethodField()

    class Meta:
        model = MaintenanceInterval
        fields = [
            "id",
            "bike",
            "assembly",
            "template",
            "kind",
            "label",
            "interval_km",
            "interval_days",
            "last_done_at",
            "notes",
            "status",
            "km_since",
            "days_since",
            "usage_ratio",
            "last_log",
        ]
        read_only_fields = ["bike", "assembly", "template"]

    def validate_last_done_at(self, value):
        if value is not None and value > ride_clock_now().date():
            raise serializers.ValidationError(
                "Das Datum darf nicht in der Zukunft liegen."
            )
        return value

    def update(self, instance, validated_data):
        # Wer das Datum korrigiert, korrigiert auch, ab wann die km zählen.
        if "last_done_at" in validated_data:
            instance.km_baseline_at = moment_for(validated_data["last_done_at"])
        return super().update(instance, validated_data)

    def _ledger(self, obj: MaintenanceInterval) -> RideLedger:
        return ride_ledger(cast(dict, self.context), obj.bike_id)

    def get_status(self, obj: MaintenanceInterval) -> str:
        return obj.status(self._ledger(obj))

    def get_km_since(self, obj: MaintenanceInterval) -> float:
        return obj.km_since_done(self._ledger(obj))

    def get_days_since(self, obj: MaintenanceInterval) -> int | None:
        if obj.last_done_at is None:
            return None
        return (date.today() - obj.last_done_at).days

    def get_usage_ratio(self, obj: MaintenanceInterval) -> float | None:
        ratio = obj.usage_ratio(self._ledger(obj))
        return round(ratio, 3) if ratio is not None else None

    def get_last_log(self, obj: MaintenanceInterval):
        log = obj.logs.first()
        return MaintenanceLogSerializer(log).data if log is not None else None


class ComponentGroupSerializer(serializers.ModelSerializer[ComponentGroup]):
    """Katalog-Blueprint mit genesteten Templates, getrennt in parts/consumables."""

    category_display = serializers.CharField(
        source="get_category_display", read_only=True
    )
    parts = serializers.SerializerMethodField()
    consumables = serializers.SerializerMethodField()
    has_active_instance = serializers.SerializerMethodField()

    class Meta:
        model = ComponentGroup
        fields = [
            "id",
            "name",
            "notes",
            "category",
            "category_display",
            "applicable_bike_types",
            "sort_order",
            "recommended",
            "is_system",
            # Steuert im Client, ob "Wechseln"/"Teile erneuern" ueberhaupt
            # angeboten werden (siehe models.py::GroupKind).
            "kind",
            "position",
            "parts",
            "consumables",
            "has_active_instance",
        ]

    def _templates(self, obj: ComponentGroup, kind: str):
        bike_type = cast(dict, self.context).get("bike_type")
        result = []
        for tpl in obj.templates.all():
            if tpl.maintenance_kind != kind:
                continue
            if bike_type and not tpl.applies_to(bike_type):
                continue
            result.append(ComponentTemplateSerializer(tpl).data)
        return result

    def get_parts(self, obj: ComponentGroup):
        return self._templates(obj, MaintenanceKind.PART)

    def get_consumables(self, obj: ComponentGroup):
        return self._templates(obj, MaintenanceKind.CONSUMABLE)

    def get_has_active_instance(self, obj: ComponentGroup) -> bool:
        """
        True, wenn das Bike bereits eine aufgezogene Instanz dieser Gruppe hat.
        Nur gesetzt, wenn der Aufrufer (`BikeAssemblyListView.get`) die IDs im
        Context mitgibt — sonst (Katalog ohne Bike-Bezug) immer False.
        """
        used_group_ids = cast(dict, self.context).get("used_group_ids")
        if used_group_ids is None:
            return False
        return obj.id in used_group_ids


class AssemblyPeriodSerializer(serializers.Serializer):
    """Eine Zeit am Rad, für den Verlauf einer Baugruppe."""

    started_at = serializers.DateTimeField()
    ended_at = serializers.DateTimeField(allow_null=True)


class BikeAssemblySerializer(serializers.ModelSerializer[BikeAssembly]):
    group_detail = ComponentGroupSerializer(source="group", read_only=True)
    display_name = serializers.CharField(read_only=True)
    # Abgeleitete Properties aus `status`, damit der Client die Kodierung nicht
    # nachbauen muss.
    is_active = serializers.BooleanField(read_only=True)
    is_parked = serializers.BooleanField(read_only=True)
    is_retired = serializers.BooleanField(read_only=True)
    slots = serializers.SerializerMethodField()
    intervals = serializers.SerializerMethodField()
    assembly_km = serializers.SerializerMethodField()
    worst_status = serializers.SerializerMethodField()
    last_used_at = serializers.SerializerMethodField()
    periods = serializers.SerializerMethodField()
    linked_assembly_ids = serializers.SerializerMethodField()
    linked_component_ids = serializers.SerializerMethodField()

    class Meta:
        model = BikeAssembly
        fields = [
            "id",
            "bike",
            "group",
            "group_detail",
            "name",
            "display_name",
            "installed_at",
            "retired_at",
            "status",
            "is_active",
            "is_parked",
            "is_retired",
            "last_used_at",
            "slots",
            "intervals",
            "assembly_km",
            "worst_status",
            "periods",
            "linked_assembly_ids",
            "linked_component_ids",
            "created_at",
            "updated_at",
        ]
        # `status`/`retired_at` bewusst read-only: ein Zustandswechsel muss über
        # activate/retire/restore laufen, sonst bliebe die Nutzungsperiode offen
        # und der abgezogene Satz sammelte weiter km. `installed_at` ändert man über
        # `bikes/{id}/installed-at/`, weil es alle Teile mit verschiebt.
        read_only_fields = [
            "bike",
            "group",
            "installed_at",
            "status",
            "retired_at",
            "created_at",
            "updated_at",
        ]

    def _ledger(self, obj: BikeAssembly) -> RideLedger:
        return ride_ledger(cast(dict, self.context), obj.bike_id)

    def get_slots(self, obj: BikeAssembly):
        slots = sorted(
            obj.slots.all(), key=lambda s: (s.template.category, s.template.name)
        )
        return ComponentSlotSerializer(slots, many=True, context=self.context).data

    def get_intervals(self, obj: BikeAssembly):
        return MaintenanceIntervalSerializer(
            obj.intervals.all(), many=True, context=self.context
        ).data

    def get_assembly_km(self, obj: BikeAssembly) -> float:
        return obj.compute_km(self._ledger(obj))

    def get_last_used_at(self, obj: BikeAssembly) -> date | None:
        """
        Wann die Baugruppe zuletzt am Rad war — bei geparkten Sätzen das Datum
        des Abziehens, damit die Wechsel-Auswahl "zuletzt gefahren" anzeigen kann.
        """
        if obj.is_active:
            return date.today()
        ends = [end.date() for _, end in assembly_windows(obj) if end is not None]
        return max(ends) if ends else obj.retired_at

    def get_periods(self, obj: BikeAssembly):
        return AssemblyPeriodSerializer(obj.periods.all(), many=True).data

    def get_worst_status(self, obj: BikeAssembly) -> str:
        return obj.worst_status(self._ledger(obj))

    def get_linked_assembly_ids(self, obj: BikeAssembly) -> list[int]:
        return [other.pk for other in obj.linked_assemblies.all()]

    def get_linked_component_ids(self, obj: BikeAssembly) -> list[int]:
        return [component.pk for component in obj.linked_components.all()]


class AssemblyActionSerializer(serializers.Serializer):
    """Body für activate/retire: optional, an welchem Tag das passiert ist."""

    on = serializers.DateField(required=False, allow_null=True)
    # Nur für retire: den ganzen Satz (das gekoppelte Laufrad) mit ausmustern.
    with_linked = serializers.BooleanField(required=False, default=False)


class AssemblyLinksSerializer(serializers.Serializer):
    """Body für PUT /assemblies/{id}/links/ — ersetzt, was mitwechselt."""

    assembly_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, default=list
    )
    component_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, default=list
    )


class MaintenanceIntervalLogRequestSerializer(serializers.Serializer):
    """Body für POST /intervals/{id}/log/ ("Erledigt/Erneuert")."""

    done_at = serializers.DateField(required=False, allow_null=True)
    note = serializers.CharField(required=False, allow_blank=True, default="")


class MaintenanceIntervalCreateSerializer(serializers.Serializer):
    """Body für POST /bikes/{id}/intervals/ (Ad-hoc-Intervall)."""

    assembly = serializers.IntegerField(required=False, allow_null=True)
    kind = serializers.ChoiceField(
        choices=MaintenanceIntervalKind.values,
        required=False,
        default=MaintenanceIntervalKind.CUSTOM,
    )
    label = serializers.CharField(max_length=100)
    interval_km = serializers.FloatField(required=False, allow_null=True, min_value=0)
    interval_days = serializers.IntegerField(
        required=False, allow_null=True, min_value=0
    )
    last_done_at = serializers.DateField(required=False)
    notes = serializers.CharField(required=False, allow_blank=True, default="")


class AssemblyPartItemSerializer(serializers.Serializer):
    template_id = serializers.IntegerField()
    include = serializers.BooleanField(default=True)
    brand = serializers.CharField(required=False, allow_blank=True, default="")
    model_name = serializers.CharField(required=False, allow_blank=True, default="")
    custom_warn_km = serializers.FloatField(
        required=False, allow_null=True, min_value=0
    )
    custom_warn_days = serializers.IntegerField(
        required=False, allow_null=True, min_value=0
    )
    # Statt eine neue Component anzulegen: eine bereits vorhandene, noch
    # ungruppierte ComponentSlot (samt montiertem Teil + Verlauf) in diese
    # Baugruppe übernehmen. brand/model_name/custom_warn_* werden dann
    # ignoriert — es ist ja dasselbe physische Teil. Siehe
    # _build_assembly_from_request/_validate_assembly_items.
    existing_slot_id = serializers.IntegerField(required=False, allow_null=True)
    # Verwandter Fall: ein Teil aus dem Regal (siehe `spare_components`) übernehmen,
    # statt es neu anzulegen. Schließt sich mit existing_slot_id aus.
    reuse_component_id = serializers.IntegerField(required=False, allow_null=True)


class AssemblyIntervalItemSerializer(serializers.Serializer):
    template_id = serializers.IntegerField()
    include = serializers.BooleanField(default=True)
    interval_km = serializers.FloatField(required=False, allow_null=True, min_value=0)
    interval_days = serializers.IntegerField(
        required=False, allow_null=True, min_value=0
    )


class AssemblyCreateRequestSerializer(serializers.Serializer):
    """Body für POST /bikes/{id}/assemblies/."""

    group_id = serializers.IntegerField(required=False)
    name = serializers.CharField(required=False, allow_blank=True, default="")
    installed_at = serializers.DateField(required=False)
    parts = AssemblyPartItemSerializer(many=True, required=False, default=list)
    intervals = AssemblyIntervalItemSerializer(many=True, required=False, default=list)
    # Neue Instanz direkt aufziehen? Ohne Angabe: ja, falls die Gruppe frei ist —
    # ist bereits eine Instanz aufgezogen (zweiter Laufradsatz), wird die neue
    # standardmäßig geparkt angelegt statt die bestehende zu verdrängen.
    activate = serializers.BooleanField(required=False, allow_null=True, default=None)
    # Gleich mitkoppeln: das andere Laufrad des neuen Satzes.
    linked_assembly_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, default=list
    )


class AssemblyAddItemRequestSerializer(serializers.Serializer):
    """Body fuer POST /maintenance/assemblies/{id}/items/."""

    template_id = serializers.IntegerField()
    brand = serializers.CharField(
        max_length=100, required=False, allow_blank=True, default=""
    )
    model_name = serializers.CharField(
        max_length=100, required=False, allow_blank=True, default=""
    )
    installed_at = serializers.DateField(required=False, allow_null=True)
    custom_warn_km = serializers.FloatField(required=False, allow_null=True)
    #: Nur fuer Verbrauchsmaterial; ohne Angabe gelten die Katalog-Werte.
    interval_km = serializers.FloatField(required=False, allow_null=True)
    interval_days = serializers.IntegerField(required=False, allow_null=True)


class BikeInstalledAtRequestSerializer(serializers.Serializer):
    """Body fuer POST /maintenance/bikes/{id}/installed-at/."""

    installed_at = serializers.DateField()
    #: Ohne assembly_id gilt das Datum fuer alle montierten Teile des Bikes.
    assembly_id = serializers.IntegerField(required=False, allow_null=True)


class AssistantModelsRequestSerializer(serializers.Serializer):
    """Body fuer POST /maintenance/assistant/models/ (Kudo, Schritt 1)."""

    manufacturer = serializers.CharField(max_length=100)
    bike_type = serializers.CharField(max_length=20, required=False, allow_blank=True)
    # Untergrenze etwa beim ersten Serienfahrrad; Obergrenze grosszuegig fuer
    # Modelljahre, die dem Kalenderjahr vorauslaufen.
    year = serializers.IntegerField(
        required=False, allow_null=True, min_value=1850, max_value=2100
    )


class AssistantSetupRequestSerializer(serializers.Serializer):
    """Body fuer POST /maintenance/bikes/{id}/assistant/setup/ (Kudo, Schritt 2)."""

    manufacturer = serializers.CharField(max_length=100)
    model = serializers.CharField(max_length=100)
    year = serializers.IntegerField(
        required=False, allow_null=True, min_value=1850, max_value=2100
    )
    # Serienausstattung aus Schritt 1, falls der Nutzer ein vorgeschlagenes Modell
    # angeklickt hat statt frei zu tippen. Ankert Schritt 2 an genau dem Rad aus der
    # Liste, statt den Modellnamen ein zweites Mal interpretieren zu lassen.
    spec = serializers.CharField(
        max_length=200, required=False, allow_blank=True, default=""
    )
