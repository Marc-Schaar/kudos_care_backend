from django.contrib import admin
from .models import Bike, ComponentTemplate, ComponentSlot, Component


@admin.register(Bike)
class BikeAdmin(admin.ModelAdmin):
    list_display = ["name", "bike_type", "athlete", "retired"]
    list_filter = ["bike_type", "retired"]
    search_fields = ["name", "strava_bike_id"]


@admin.register(ComponentTemplate)
class ComponentTemplateAdmin(admin.ModelAdmin):
    list_display = ["name", "category", "warn_km", "warn_days", "is_system"]
    list_filter = ["category", "is_system"]
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


@admin.register(ComponentSlot)
class ComponentSlotAdmin(admin.ModelAdmin):
    list_display = ["bike", "display_name", "mounted_component"]
    list_filter = ["bike__bike_type"]
    inlines = [ComponentInline]

    def display_name(self, obj):
        return obj.display_name


@admin.register(Component)
class ComponentAdmin(admin.ModelAdmin):
    list_display = ["__str__", "installed_at", "is_mounted", "distance_at_install"]
    list_filter = ["is_mounted", "slot__bike__bike_type"]
