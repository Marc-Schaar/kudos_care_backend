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
    ComponentTemplate,
    MaintenanceInterval,
    MaintenanceIntervalKind,
    MaintenanceKind,
    MaintenanceLog,
    WarnLevel,
    warn_status_from_ratio,
    worst_of,
)

from .usage import component_active_km


def bike_total_km(context: dict, bike: Bike) -> float | None:
    """
    Gesamtkilometer eines Bikes — einmal je Request bestimmt, nicht je Slot.

    `Bike.total_distance_km` ist ein Property mit eigener Aggregat-Query. Ohne
    diesen Cache liest jeder Slot und jedes Intervall es erneut; in der
    Bike-Liste waren das gemessen 18 identische `SUM`-Queries pro Bike.

    Views, die genau ein Bike serialisieren, legen den Wert vorab als
    `context["bike_total_km"]` ab (siehe `BikeAssemblySerializer` und
    `api/views.py`); dieser Schlüssel hat Vorrang. Sonst wird je Bike-PK
    gecacht, damit ein Context über mehrere Bikes hinweg nicht die Zahl des
    falschen Bikes zurückgibt.
    """
    if "bike_total_km" in context:
        return context["bike_total_km"]
    key = f"_bike_km_{bike.pk}"
    if key not in context:
        context[key] = bike.total_distance_km
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
    component: Component, bike_total_km: float | None, as_of: date | None = None
) -> dict:
    """
    Berechnet Verschleiß und Warn-Status für eine montierte Komponente.
    Gibt ein Dict zurück das direkt im Serializer verwendet wird.

    `wear_km`/`wear_days` sind informative Totalwerte seit Einbau. Der
    Warn-Status wird dagegen — falls eine Prüfung (ComponentCheck) vorliegt —
    ab dem Zeitpunkt der letzten Prüfung neu berechnet ("Freigeben"), mit dem
    dabei angegebenen Snooze-Intervall (falls keins angegeben wurde, gilt ab
    der Prüfung wieder die normale empfohlene/individuelle Lebensdauer).

    Die km-Achse rechnet über `api/usage.py` nur die Abschnitte, in denen die
    Baugruppe tatsächlich am Rad war — ein abgezogener Laufradsatz sammelt keine
    km, während das Bike auf dem anderen Satz weiterfährt. Die Tage-Achse zählt
    bewusst durchgehend weiter (Gummi/Dichtmilch altern auch im Keller).

    `as_of` erlaubt eine Projektion auf ein zukünftiges Datum (siehe
    app_notifications — "voraussichtlich unsafe bei nächster Fahrt"): nur die
    Tage-Achse wird dadurch wirklich projiziert, `bike_total_km` bleibt der
    übergebene (heutige) Wert, da zukünftige Distanz nicht bekannt ist. Default
    (None) = heute, identisch zum bisherigen Verhalten.
    """
    as_of = as_of or date.today()
    warn_km = component.effective_warn_km
    warn_days = component.effective_warn_days

    result = {
        "wear_km": None,
        "wear_days": None,
        "warn_status_km": WarnLevel.UNKNOWN,
        "warn_status_days": WarnLevel.UNKNOWN,
        "warn_status_weather_km": WarnLevel.UNKNOWN,
        "warn_status_overall": WarnLevel.UNKNOWN,
    }

    # ── km-Verschleiß (informativ, seit Einbau, ohne Parkzeiten) ──────────────
    # + carried_over_wear_km: km, die vor einem früheren Ausbau schon aufgelaufen
    # waren (siehe Component.carried_over_wear_km) — component_active_km() sieht
    # nur die Perioden der *aktuellen* Baugruppe, eine reaktivierte Komponente
    # würde sonst wieder bei 0 anfangen.
    active_km = component_active_km(component, bike_total_km)
    carried_km = component.carried_over_wear_km
    if active_km is None and carried_km is None:
        result["wear_km"] = None
    else:
        result["wear_km"] = round((active_km or 0.0) + (carried_km or 0.0), 1)

    # ── Tage-Verschleiß (informativ, seit Einbau) ─────────────────────────────
    if component.installed_at:
        result["wear_days"] = (as_of - component.installed_at).days

    # ── Status-Baseline: letzte Prüfung falls vorhanden, sonst Einbau ────────
    latest_check = component.checks.first()

    if latest_check is not None:
        km_since_check = (
            component_active_km(
                component,
                bike_total_km,
                since_km=latest_check.checked_at_distance_km,
            )
            if latest_check.checked_at_distance_km is not None
            else None
        )
        if km_since_check is not None:
            threshold_km = latest_check.snooze_km or warn_km
            if threshold_km:
                result["warn_status_km"] = warn_status_from_ratio(
                    km_since_check / threshold_km
                )
            else:
                result["warn_status_km"] = WarnLevel.OK

        days_since_check = (as_of - latest_check.checked_at).days
        threshold_days = latest_check.snooze_days or warn_days
        if threshold_days:
            result["warn_status_days"] = warn_status_from_ratio(
                days_since_check / threshold_days
            )
        else:
            result["warn_status_days"] = WarnLevel.OK
    else:
        if result["wear_km"] is not None:
            if warn_km:
                result["warn_status_km"] = warn_status_from_ratio(
                    result["wear_km"] / warn_km
                )
            else:
                result["warn_status_km"] = WarnLevel.OK

        if result["wear_days"] is not None:
            if warn_days:
                result["warn_status_days"] = warn_status_from_ratio(
                    result["wear_days"] / warn_days
                )
            else:
                result["warn_status_days"] = WarnLevel.OK

    # ── Wetter-gewichteter km-Verschleiß ──────────────────────────────────────
    # component.weather_wear_km wird asynchron von WeatherWearService befüllt
    # (siehe app_maintenance/api/services.py), nicht hier live berechnet — ist
    # aber immer der volle Verlauf seit Einbau. Liegt eine Prüfung vor, wird
    # daher analog zur km-/Tage-Achse ab der bei der Prüfung gespeicherten
    # Baseline (checked_at_weather_wear_km) gerechnet, sonst bleibt eine
    # Freigabe für diese Achse wirkungslos und die Komponente erscheint trotz
    # "Freigeben" weiter als überfällig.
    if component.weather_wear_km is not None:
        if (
            latest_check is not None
            and latest_check.checked_at_weather_wear_km is not None
        ):
            weather_km_since_check = (
                component.weather_wear_km - latest_check.checked_at_weather_wear_km
            )
        else:
            weather_km_since_check = component.weather_wear_km

        if warn_km:
            result["warn_status_weather_km"] = warn_status_from_ratio(
                weather_km_since_check / warn_km
            )
        else:
            result["warn_status_weather_km"] = WarnLevel.OK

    # ── Gesamt-Status = schlechtester Einzelwert ──────────────────────────────
    result["warn_status_overall"] = worst_of(
        [
            result["warn_status_km"],
            result["warn_status_days"],
            result["warn_status_weather_km"],
        ]
    )

    return result


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
            "notes",
            "group",
            "group_name",
        ]
        read_only_fields = ["is_system"]


class ComponentCheckSerializer(serializers.ModelSerializer[ComponentCheck]):
    """Kompakte, read-only Zusammenfassung der letzten Prüfung."""

    class Meta:
        model = ComponentCheck
        fields = [
            "id",
            "checked_at",
            "checked_at_distance_km",
            "checked_at_weather_wear_km",
            "condition_pct",
            "snooze_km",
            "snooze_days",
            "note",
        ]


class ComponentSerializer(serializers.ModelSerializer[Component]):
    wear_km = serializers.SerializerMethodField()
    wear_days = serializers.SerializerMethodField()
    warn_status_km = serializers.SerializerMethodField()
    warn_status_days = serializers.SerializerMethodField()
    warn_status_weather_km = serializers.SerializerMethodField()
    warn_status_overall = serializers.SerializerMethodField()
    last_check = serializers.SerializerMethodField()

    class Meta:
        model = Component
        fields = [
            "id",
            "slot",
            "brand",
            "model_name",
            "distance_at_install",
            "installed_at",
            "retired_at",
            "is_mounted",
            "notes",
            "custom_warn_km",
            "custom_warn_days",
            "created_at",
            "updated_at",
            "wear_km",
            "wear_days",
            "warn_status_km",
            "warn_status_days",
            "weather_wear_km",
            "weather_wear_computed_at",
            "weather_wear_ride_count",
            "warn_status_weather_km",
            "warn_status_overall",
            "last_check",
        ]
        read_only_fields = [
            "slot",
            "created_at",
            "updated_at",
            "weather_wear_km",
            "weather_wear_computed_at",
            "weather_wear_ride_count",
        ]

    def get_last_check(self, obj):
        latest_check = obj.checks.first()
        if latest_check is None:
            return None
        return ComponentCheckSerializer(latest_check).data

    def _get_wear(self, obj: Component) -> dict:
        """Wear-Dict einmal berechnen und im Serializer-Context cachen."""
        context = cast(dict, self.context)
        cache_key = f"_wear_{obj.pk}"
        if cache_key not in context:
            bike = obj.slot.bike
            context[cache_key] = compute_wear(obj, bike_total_km(context, bike))
        return context[cache_key]

    def get_wear_km(self, obj):
        return self._get_wear(obj)["wear_km"]

    def get_wear_days(self, obj):
        return self._get_wear(obj)["wear_days"]

    def get_warn_status_km(self, obj):
        return self._get_wear(obj)["warn_status_km"]

    def get_warn_status_days(self, obj):
        return self._get_wear(obj)["warn_status_days"]

    def get_warn_status_weather_km(self, obj):
        return self._get_wear(obj)["warn_status_weather_km"]

    def get_warn_status_overall(self, obj):
        return self._get_wear(obj)["warn_status_overall"]

    def validate(self, attrs):
        """Stellt sicher dass is_mounted=True nicht doppelt vergeben wird."""
        is_mounted = attrs.get(
            "is_mounted", getattr(self.instance, "is_mounted", False)
        )
        slot = attrs.get("slot", getattr(self.instance, "slot", None))

        if is_mounted and slot:
            qs = Component.objects.filter(slot=slot, is_mounted=True)
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError(
                    {
                        "is_mounted": "In diesem Slot ist bereits eine Komponente montiert."
                    }
                )
        return attrs


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


class ComponentMountSerializer(serializers.Serializer):
    """
    Vereinfachter Serializer für die mount/unmount-Aktionen.
    POST /slots/{id}/mount/   → montiert eine vorhandene Component
    POST /slots/{id}/unmount/ → baut die montierte Komponente aus
    """

    component_id = serializers.IntegerField(required=False)


class ComponentSlotSerializer(serializers.ModelSerializer[ComponentSlot]):
    display_name = serializers.CharField(read_only=True)
    template_detail = ComponentTemplateSerializer(source="template", read_only=True)
    mounted_component = serializers.SerializerMethodField()
    components = ComponentSerializer(many=True, read_only=True)

    warn_status = serializers.SerializerMethodField()

    class Meta:
        model = ComponentSlot
        fields = [
            "id",
            "bike",
            "template",
            "template_detail",
            "custom_name",
            "display_name",
            "warn_status",
            "mounted_component",
            "components",
        ]
        read_only_fields = ["bike", "display_name"]

    def get_mounted_component(self, obj: ComponentSlot):
        comp = obj.mounted_component
        if comp is None:
            return None
        return ComponentSerializer(comp, context=self.context).data

    def get_warn_status(self, obj: ComponentSlot) -> str:
        comp = obj.mounted_component
        if comp is None:
            return WarnLevel.UNKNOWN
        wear = compute_wear(comp, bike_total_km(cast(dict, self.context), obj.bike))
        return wear["warn_status_overall"]


class ComponentSlotListSerializer(serializers.ModelSerializer[ComponentSlot]):
    """
    Kompakte Variante für Listen — ohne verschachtelte Components.
    """

    display_name = serializers.CharField(read_only=True)
    category = serializers.CharField(source="template.category", read_only=True)
    category_display = serializers.CharField(
        source="template.get_category_display", read_only=True
    )
    template_detail = ComponentTemplateSerializer(source="template", read_only=True)
    warn_status = serializers.SerializerMethodField()
    mounted_component = serializers.SerializerMethodField()

    class Meta:
        model = ComponentSlot
        fields = [
            "id",
            "bike",
            "template",
            "template_detail",
            "display_name",
            "category",
            "category_display",
            "warn_status",
            "mounted_component",
        ]

    def _get_wear(self, comp: Component, obj: ComponentSlot) -> dict:
        """Wear-Dict einmal berechnen und im Serializer-Context cachen (wird von
        get_warn_status und get_mounted_component gemeinsam genutzt)."""
        context = cast(dict, self.context)
        cache_key = f"_wear_{comp.pk}"
        if cache_key not in context:
            context[cache_key] = compute_wear(comp, bike_total_km(context, obj.bike))
        return context[cache_key]

    def get_warn_status(self, obj: ComponentSlot) -> str:
        comp = obj.mounted_component
        if comp is None:
            return WarnLevel.UNKNOWN
        return self._get_wear(comp, obj)["warn_status_overall"]

    def get_mounted_component(self, obj: ComponentSlot):
        comp = obj.mounted_component
        if comp is None:
            return None
        latest_check = comp.checks.first()
        wear = self._get_wear(comp, obj)
        return {
            "id": comp.id,
            "brand": comp.brand,
            "model_name": comp.model_name,
            "installed_at": comp.installed_at,
            "condition_pct": latest_check.condition_pct if latest_check else None,
            "wear_km": wear["wear_km"],
            "wear_days": wear["wear_days"],
            "effective_warn_km": comp.effective_warn_km,
            "effective_warn_days": comp.effective_warn_days,
            "warn_status_overall": wear["warn_status_overall"],
            "weather_wear_km": comp.weather_wear_km,
            "weather_wear_ride_count": comp.weather_wear_ride_count,
            "warn_status_weather_km": wear["warn_status_weather_km"],
        }


class SpareComponentSerializer(serializers.ModelSerializer[Component]):
    """
    Ein ausgebautes Teil als Übernahme-Vorschlag beim Baugruppe-Anlegen (siehe
    `_spare_components_by_template`) — Pendant zu `ComponentSlotListSerializer`s
    `mounted_component` für den Fall, dass das Teil gerade *nicht* montiert ist.
    """

    template = serializers.IntegerField(source="slot.template_id", read_only=True)
    prior_wear_km = serializers.SerializerMethodField()

    class Meta:
        model = Component
        fields = [
            "id",
            "template",
            "brand",
            "model_name",
            "installed_at",
            "retired_at",
            "distance_at_retire",
            "prior_wear_km",
        ]

    def get_prior_wear_km(self, obj: Component) -> float | None:
        """
        Bereits gefahrene km bis zum Ausbau — informativ für den Übernahme-
        Vorschlag ("dieses Teil hat schon X km"). Wird bei tatsächlicher
        Übernahme (`reuse_component_id`) 1:1 zu `carried_over_wear_km`
        (plus bereits vorher eingefrorenem Altverschleiß, falls vorhanden).
        """
        if obj.distance_at_install is None or obj.distance_at_retire is None:
            return obj.carried_over_wear_km
        ridden = obj.distance_at_retire - obj.distance_at_install
        return round(ridden + (obj.carried_over_wear_km or 0.0), 1)


class BikeSerializer(serializers.ModelSerializer[Bike]):
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
        return ComponentSlotListSerializer(
            slots_on_bike(obj), many=True, context=self.context
        ).data

    def get_warn_status(self, obj: Bike) -> str:
        total_km = bike_total_km(cast(dict, self.context), obj)
        statuses = []

        for slot in slots_on_bike(obj):
            comp = slot.mounted_component
            if comp is None:
                statuses.append(WarnLevel.UNKNOWN)
                continue
            statuses.append(compute_wear(comp, total_km)["warn_status_overall"])

        for interval in obj.intervals.all():
            statuses.append(interval.status(total_km))

        return worst_of(statuses)


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
        total_km = bike_total_km(cast(dict, self.context), obj)
        statuses = []
        for slot in slots_on_bike(obj):
            comp = slot.mounted_component
            if comp is None:
                continue
            statuses.append(compute_wear(comp, total_km)["warn_status_overall"])
        for interval in obj.intervals.all():
            statuses.append(interval.status(total_km))
        return worst_of(statuses)


# ── Baugruppen (BikeAssembly) & Wartungs-Intervalle ───────────────────────────


class MaintenanceLogSerializer(serializers.ModelSerializer[MaintenanceLog]):
    class Meta:
        model = MaintenanceLog
        fields = ["id", "done_at", "done_distance_km", "note", "created_at"]


class MaintenanceIntervalSerializer(serializers.ModelSerializer[MaintenanceInterval]):
    """
    Intervall-Status ohne "Zustand". `status`/km_since/days_since brauchen den
    aktuellen km-Stand des Bikes — wird über den Context (`bike_total_km`)
    gereicht, Fallback ist `obj.bike.total_distance_km`.
    """

    status = serializers.SerializerMethodField()
    km_since = serializers.SerializerMethodField()
    days_since = serializers.SerializerMethodField()
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
            "last_done_distance_km",
            "notes",
            "status",
            "km_since",
            "days_since",
            "last_log",
        ]
        read_only_fields = ["bike", "assembly", "template"]

    def _bike_total_km(self, obj: MaintenanceInterval) -> float | None:
        return bike_total_km(cast(dict, self.context), obj.bike)

    def get_status(self, obj: MaintenanceInterval) -> str:
        return obj.status(self._bike_total_km(obj))

    def get_km_since(self, obj: MaintenanceInterval) -> float | None:
        total = self._bike_total_km(obj)
        if total is None or obj.last_done_distance_km is None:
            return None
        return round(total - obj.last_done_distance_km, 1)

    def get_days_since(self, obj: MaintenanceInterval) -> int | None:
        if obj.last_done_at is None:
            return None
        return (date.today() - obj.last_done_at).days

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


class BikeAssemblySerializer(serializers.ModelSerializer[BikeAssembly]):
    group_detail = ComponentGroupSerializer(source="group", read_only=True)
    display_name = serializers.CharField(read_only=True)
    # Seit `status` das einzige Zustandsfeld ist, sind beide abgeleitete
    # Properties. Sie bleiben im Response, weil das Frontend-Model sie
    # voraussetzt; schreibbar war ohnehin nur `status`-freier Kram (siehe
    # BikeAssemblyDetailView).
    is_active = serializers.BooleanField(read_only=True)
    is_parked = serializers.BooleanField(read_only=True)
    slots = serializers.SerializerMethodField()
    intervals = serializers.SerializerMethodField()
    assembly_km = serializers.SerializerMethodField()
    worst_status = serializers.SerializerMethodField()
    last_used_at = serializers.SerializerMethodField()

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
            "last_used_at",
            "slots",
            "intervals",
            "assembly_km",
            "worst_status",
            "created_at",
            "updated_at",
        ]
        # `status`/`retired_at` bewusst read-only: ein Zustandswechsel muss über
        # activate/retire/swap laufen. Per PATCH gesetzt bliebe die
        # AssemblyUsagePeriod offen und der abgezogene Satz sammelte weiter km.
        read_only_fields = [
            "bike",
            "group",
            "status",
            "retired_at",
            "created_at",
            "updated_at",
        ]

    def _bike_total_km(self, obj: BikeAssembly) -> float | None:
        return bike_total_km(cast(dict, self.context), obj.bike)

    def get_slots(self, obj: BikeAssembly):
        slots = sorted(
            obj.slots.all(), key=lambda s: (s.template.category, s.template.name)
        )
        return ComponentSlotListSerializer(slots, many=True, context=self.context).data

    def get_intervals(self, obj: BikeAssembly):
        return MaintenanceIntervalSerializer(
            obj.intervals.all(), many=True, context=self.context
        ).data

    def get_assembly_km(self, obj: BikeAssembly) -> float | None:
        return obj.compute_km(self._bike_total_km(obj))

    def get_last_used_at(self, obj: BikeAssembly) -> date | None:
        """
        Wann die Baugruppe zuletzt am Rad war — bei geparkten Sätzen das Datum
        des Abziehens, damit die Wechsel-Auswahl "zuletzt gefahren" anzeigen kann.
        """
        if obj.is_active:
            return date.today()
        ends = [p.ended_at for p in obj.periods.all() if p.ended_at is not None]
        return max(ends) if ends else obj.retired_at

    def get_worst_status(self, obj: BikeAssembly) -> str:
        return obj.worst_status(self._bike_total_km(obj))


class MaintenanceIntervalLogRequestSerializer(serializers.Serializer):
    """Body für POST /intervals/{id}/log/ ("Erledigt/Erneuert")."""

    done_at = serializers.DateField(required=False)
    done_distance_km = serializers.FloatField(
        required=False, allow_null=True, min_value=0
    )
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
    # Verwandter Fall: ein bereits *ausgebautes* Teil (z.B. der zurückgelegte
    # Laufradsatz aus dem Keller, siehe `spare_components`) reaktivieren, statt
    # es neu anzulegen. Schließt sich mit existing_slot_id aus.
    reuse_component_id = serializers.IntegerField(required=False, allow_null=True)


class AssemblyIntervalItemSerializer(serializers.Serializer):
    template_id = serializers.IntegerField()
    include = serializers.BooleanField(default=True)
    interval_km = serializers.FloatField(required=False, allow_null=True, min_value=0)
    interval_days = serializers.IntegerField(
        required=False, allow_null=True, min_value=0
    )


class AssemblyCreateRequestSerializer(serializers.Serializer):
    """Body für POST /bikes/{id}/assemblies/ und /assemblies/{id}/swap/."""

    group_id = serializers.IntegerField(required=False)
    name = serializers.CharField(required=False, allow_blank=True, default="")
    installed_at = serializers.DateField(required=False)
    parts = AssemblyPartItemSerializer(many=True, required=False, default=list)
    intervals = AssemblyIntervalItemSerializer(many=True, required=False, default=list)
    # Neue Instanz direkt aufziehen? Ohne Angabe: ja, falls die Gruppe frei ist —
    # ist bereits eine Instanz aufgezogen (zweiter Laufradsatz), wird die neue
    # standardmäßig geparkt angelegt statt die bestehende zu verdrängen.
    activate = serializers.BooleanField(required=False, allow_null=True, default=None)


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
