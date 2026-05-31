from datetime import date
from rest_framework import serializers
from app_maintenance.models import Bike, ComponentTemplate, ComponentSlot, Component


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
    """
    template = component.slot.template
    result = {
        "wear_km": None,
        "wear_days": None,
        "warn_status_km": WarnStatus.UNKNOWN,
        "warn_status_days": WarnStatus.UNKNOWN,
        "warn_status_overall": WarnStatus.UNKNOWN,
    }

    # ── km-Verschleiß ────────────────────────────────────────────────────────
    if bike_total_km is not None and component.distance_at_install is not None:
        wear_km = bike_total_km - component.distance_at_install
        result["wear_km"] = round(wear_km, 1)

        if template.warn_km:
            result["warn_status_km"] = WarnStatus.from_ratio(wear_km / template.warn_km)
        else:
            result["warn_status_km"] = WarnStatus.OK

    # ── Tage-Verschleiß ──────────────────────────────────────────────────────
    if component.installed_at:
        wear_days = (date.today() - component.installed_at).days
        result["wear_days"] = wear_days

        if template.warn_days:
            result["warn_status_days"] = WarnStatus.from_ratio(
                wear_days / template.warn_days
            )
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
            "notes",
        ]
        read_only_fields = ["is_system"]


class ComponentSerializer(serializers.ModelSerializer):
    wear_km = serializers.SerializerMethodField()
    wear_days = serializers.SerializerMethodField()
    warn_status_km = serializers.SerializerMethodField()
    warn_status_days = serializers.SerializerMethodField()
    warn_status_overall = serializers.SerializerMethodField()

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
            "created_at",
            "updated_at",
            "wear_km",
            "wear_days",
            "warn_status_km",
            "warn_status_days",
            "warn_status_overall",
        ]
        read_only_fields = ["created_at", "updated_at"]

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
        read_only_fields = ["display_name"]

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
    warn_status = serializers.SerializerMethodField()
    mounted_component = serializers.SerializerMethodField()

    class Meta:
        model = ComponentSlot
        fields = [
            "id",
            "bike",
            "template",
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
        return {
            "id": comp.id,
            "brand": comp.brand,
            "model_name": comp.model_name,
            "installed_at": comp.installed_at,
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
