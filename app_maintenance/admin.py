from django.contrib import admin
from .models import (
    Bike,
    ComponentGroup,
    ComponentTemplate,
    ComponentSlot,
    Component,
    ComponentCheck,
    WeatherSensitivityCoefficient,
)


@admin.register(ComponentGroup)
class ComponentGroupAdmin(admin.ModelAdmin):
    list_display = ["name"]
    search_fields = ["name"]


@admin.register(Bike)
class BikeAdmin(admin.ModelAdmin):
    list_display = ["name", "bike_type", "athlete", "retired", "total_distance_km"]
    list_filter = ["bike_type", "retired"]
    search_fields = ["name", "strava_bike_id", "athlete__strava_athlete_id"]
    raw_id_fields = ["athlete"]
    readonly_fields = ["created_at", "updated_at", "condition_report", "condition_report_generated_at"]

    @admin.display(description="Gesamt-km")
    def total_distance_km(self, obj):
        km = obj.total_distance_km
        return round(km, 1) if km is not None else None


@admin.register(ComponentTemplate)
class ComponentTemplateAdmin(admin.ModelAdmin):
    list_display = ["name", "category", "group", "warn_km", "warn_days", "is_system"]
    list_filter = ["category", "group", "is_system", "supports_condition_estimate"]
    search_fields = ["name"]


class ComponentInline(admin.TabularInline):
    model = Component
    extra = 0
    fields = [
        "brand",
        "model_name",
        "is_mounted",
        "installed_at",
        "distance_at_install",
    ]
    show_change_link = True


@admin.register(ComponentSlot)
class ComponentSlotAdmin(admin.ModelAdmin):
    list_display = ["bike", "display_name", "mounted_component"]
    list_filter = ["bike__bike_type", "template__category"]
    search_fields = ["custom_name", "bike__name", "template__name"]
    raw_id_fields = ["bike"]
    inlines = [ComponentInline]

    def display_name(self, obj):
        return obj.display_name


class ComponentCheckInline(admin.TabularInline):
    model = ComponentCheck
    extra = 0
    fields = ["checked_at", "condition_pct", "checked_at_distance_km", "snooze_km", "snooze_days", "note"]
    ordering = ["-checked_at", "-id"]


@admin.register(Component)
class ComponentAdmin(admin.ModelAdmin):
    list_display = [
        "__str__",
        "installed_at",
        "is_mounted",
        "distance_at_install",
        "weather_wear_km",
    ]
    list_filter = ["is_mounted", "slot__bike__bike_type", "slot__template__category"]
    search_fields = ["brand", "model_name", "slot__bike__name", "slot__template__name"]
    raw_id_fields = ["slot"]
    inlines = [ComponentCheckInline]

    fieldsets = [
        (
            None,
            {
                "fields": [
                    "slot",
                    "brand",
                    "model_name",
                    "is_mounted",
                    "installed_at",
                    "retired_at",
                    "distance_at_install",
                    "notes",
                ]
            },
        ),
        (
            "Individuelle Lebensdauer",
            {"fields": ["custom_warn_km", "custom_warn_days"], "classes": ["collapse"]},
        ),
        (
            "Wetter-gewichteter Verschleiß (async berechnet)",
            {
                "fields": [
                    "weather_wear_km",
                    "weather_wear_ride_count",
                    "weather_wear_computed_at",
                    "weather_wear_explanation",
                    "weather_wear_explanation_generated_at",
                ],
                "classes": ["collapse"],
            },
        ),
        (
            "KI-Prüfanleitung (Cache)",
            {
                "fields": [
                    "check_instructions",
                    "check_instructions_status",
                    "check_instructions_generated_at",
                ],
                "classes": ["collapse"],
            },
        ),
    ]
    readonly_fields = [
        "weather_wear_km",
        "weather_wear_ride_count",
        "weather_wear_computed_at",
        "weather_wear_explanation_generated_at",
        "check_instructions_status",
        "check_instructions_generated_at",
    ]


@admin.register(ComponentCheck)
class ComponentCheckAdmin(admin.ModelAdmin):
    list_display = ["component", "checked_at", "condition_pct", "checked_at_distance_km", "snooze_km", "snooze_days"]
    list_filter = ["checked_at"]
    search_fields = ["component__brand", "component__model_name", "component__slot__bike__name"]
    raw_id_fields = ["component"]
    readonly_fields = ["created_at"]


@admin.register(WeatherSensitivityCoefficient)
class WeatherSensitivityCoefficientAdmin(admin.ModelAdmin):
    list_display = [
        "category",
        "rain_sensitivity",
        "heat_sensitivity",
        "cold_sensitivity",
        "wind_sensitivity",
        "last_calibrated_at",
        "calibration_sample_count",
    ]
    list_filter = ["category"]
    readonly_fields = ["updated_at"]
