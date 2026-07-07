from datetime import date
from rest_framework import serializers
from app_maintenance.models import (
    Bike,
    ComponentTemplate,
    ComponentSlot,
    Component,
    ComponentCheck,
)


class WarnStatus:
    OK = "ok"
    WARN = "warn"
    CRITICAL = "critical"
    UNKNOWN = "unknown"

    @staticmethod
    def from_ratio(ratio: float | None) -> str:
        """
        ratio = aktueller Wert / Grenzwert (z.B. gefahrene km / warn_km)
        < 0.8  → ok
        0.8–1.0 → warn
        > 1.0  → critical
        """
        if ratio is None:
            return WarnStatus.UNKNOWN
        if ratio >= 1.0:
            return WarnStatus.CRITICAL
        if ratio >= 0.8:
            return WarnStatus.WARN
        return WarnStatus.OK


def compute_wear(component: Component, bike_total_km: float | None) -> dict:
    """
    Berechnet Verschleiß und Warn-Status für eine montierte Komponente.
    Gibt ein Dict zurück das direkt im Serializer verwendet wird.

    `wear_km`/`wear_days` sind informative Totalwerte seit Einbau. Der
    Warn-Status wird dagegen — falls eine Prüfung (ComponentCheck) vorliegt —
    ab dem Zeitpunkt der letzten Prüfung neu berechnet ("Freigeben"), mit dem
    dabei angegebenen Snooze-Intervall (falls keins angegeben wurde, gilt ab
    der Prüfung wieder die normale empfohlene/individuelle Lebensdauer).
    """
    warn_km = component.effective_warn_km
    warn_days = component.effective_warn_days

    result = {
        "wear_km": None,
        "wear_days": None,
        "warn_status_km": WarnStatus.UNKNOWN,
        "warn_status_days": WarnStatus.UNKNOWN,
        "warn_status_overall": WarnStatus.UNKNOWN,
    }

    # ── km-Verschleiß (informativ, seit Einbau) ───────────────────────────────
    if bike_total_km is not None and component.distance_at_install is not None:
        result["wear_km"] = round(bike_total_km - component.distance_at_install, 1)

    # ── Tage-Verschleiß (informativ, seit Einbau) ─────────────────────────────
    if component.installed_at:
        result["wear_days"] = (date.today() - component.installed_at).days

    # ── Status-Baseline: letzte Prüfung falls vorhanden, sonst Einbau ────────
    latest_check = component.checks.first()

    if latest_check is not None:
        if bike_total_km is not None and latest_check.checked_at_distance_km is not None:
            km_since_check = bike_total_km - latest_check.checked_at_distance_km
            threshold_km = latest_check.snooze_km or warn_km
            if threshold_km:
                result["warn_status_km"] = WarnStatus.from_ratio(km_since_check / threshold_km)
            else:
                result["warn_status_km"] = WarnStatus.OK

        days_since_check = (date.today() - latest_check.checked_at).days
        threshold_days = latest_check.snooze_days or warn_days
        if threshold_days:
            result["warn_status_days"] = WarnStatus.from_ratio(days_since_check / threshold_days)
        else:
            result["warn_status_days"] = WarnStatus.OK
    else:
        if result["wear_km"] is not None:
            if warn_km:
                result["warn_status_km"] = WarnStatus.from_ratio(result["wear_km"] / warn_km)
            else:
                result["warn_status_km"] = WarnStatus.OK

        if result["wear_days"] is not None:
            if warn_days:
                result["warn_status_days"] = WarnStatus.from_ratio(result["wear_days"] / warn_days)
            else:
                result["warn_status_days"] = WarnStatus.OK

    # ── Gesamt-Status = schlechtester Einzelwert ──────────────────────────────
    statuses = [result["warn_status_km"], result["warn_status_days"]]
    priority = [WarnStatus.CRITICAL, WarnStatus.WARN, WarnStatus.OK, WarnStatus.UNKNOWN]
    for status in priority:
        if status in statuses:
            result["warn_status_overall"] = status
            break

    return result


class ComponentTemplateSerializer(serializers.ModelSerializer):
    category_display = serializers.CharField(
        source="get_category_display", read_only=True
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
            "notes",
        ]
        read_only_fields = ["is_system"]


class ComponentCheckSerializer(serializers.ModelSerializer):
    """Kompakte, read-only Zusammenfassung der letzten Prüfung."""

    class Meta:
        model = ComponentCheck
        fields = [
            "id",
            "checked_at",
            "checked_at_distance_km",
            "condition_pct",
            "snooze_km",
            "snooze_days",
            "note",
        ]


class ComponentSerializer(serializers.ModelSerializer):
    wear_km = serializers.SerializerMethodField()
    wear_days = serializers.SerializerMethodField()
    warn_status_km = serializers.SerializerMethodField()
    warn_status_days = serializers.SerializerMethodField()
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
            "warn_status_overall",
            "last_check",
        ]
        read_only_fields = ["slot", "created_at", "updated_at"]

    def get_last_check(self, obj):
        latest_check = obj.checks.first()
        if latest_check is None:
            return None
        return ComponentCheckSerializer(latest_check).data

    def _get_wear(self, obj: Component) -> dict:
        """Wear-Dict einmal berechnen und im Serializer-Context cachen."""
        cache_key = f"_wear_{obj.pk}"
        if cache_key not in self.context:
            bike = obj.slot.bike
            self.context[cache_key] = compute_wear(obj, bike.total_distance_km)
        return self.context[cache_key]

    def get_wear_km(self, obj):
        return self._get_wear(obj)["wear_km"]

    def get_wear_days(self, obj):
        return self._get_wear(obj)["wear_days"]

    def get_warn_status_km(self, obj):
        return self._get_wear(obj)["warn_status_km"]

    def get_warn_status_days(self, obj):
        return self._get_wear(obj)["warn_status_days"]

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
        if attrs.get("condition_pct") is not None and not component.slot.template.supports_condition_estimate:
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


class ComponentSlotSerializer(serializers.ModelSerializer):
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
            return WarnStatus.UNKNOWN
        wear = compute_wear(comp, obj.bike.total_distance_km)
        return wear["warn_status_overall"]


class ComponentSlotListSerializer(serializers.ModelSerializer):
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

    def get_warn_status(self, obj: ComponentSlot) -> str:
        comp = obj.mounted_component
        if comp is None:
            return WarnStatus.UNKNOWN
        wear = compute_wear(comp, obj.bike.total_distance_km)
        return wear["warn_status_overall"]

    def get_mounted_component(self, obj: ComponentSlot):
        comp = obj.mounted_component
        if comp is None:
            return None
        latest_check = comp.checks.first()
        return {
            "id": comp.id,
            "brand": comp.brand,
            "model_name": comp.model_name,
            "installed_at": comp.installed_at,
            "condition_pct": latest_check.condition_pct if latest_check else None,
        }


class BikeSerializer(serializers.ModelSerializer):
    bike_type_display = serializers.CharField(
        source="get_bike_type_display", read_only=True
    )
    total_distance_km = serializers.SerializerMethodField()
    slots = ComponentSlotListSerializer(many=True, read_only=True)

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
        return obj.total_distance_km

    def get_warn_status(self, obj: Bike) -> str:
        priority = [
            WarnStatus.CRITICAL,
            WarnStatus.WARN,
            WarnStatus.OK,
            WarnStatus.UNKNOWN,
        ]
        slot_statuses = []

        for slot in obj.slots.all():
            comp = slot.mounted_component
            if comp is None:
                slot_statuses.append(WarnStatus.UNKNOWN)
                continue
            wear = compute_wear(comp, obj.total_distance_km)
            slot_statuses.append(wear["warn_status_overall"])

        for status in priority:
            if status in slot_statuses:
                return status
        return WarnStatus.UNKNOWN


class BikeListSerializer(serializers.ModelSerializer):
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
        return obj.total_distance_km

    def get_warn_status(self, obj: Bike) -> str:
        priority = [
            WarnStatus.CRITICAL,
            WarnStatus.WARN,
            WarnStatus.OK,
            WarnStatus.UNKNOWN,
        ]
        slot_statuses = []
        for slot in obj.slots.all():
            comp = slot.mounted_component
            if comp is None:
                continue
            wear = compute_wear(comp, obj.total_distance_km)
            slot_statuses.append(wear["warn_status_overall"])
        for status in priority:
            if status in slot_statuses:
                return status
        return WarnStatus.UNKNOWN
