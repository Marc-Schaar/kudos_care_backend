from django.db import migrations

# Heuristische Startwerte (siehe Implementierungsplan "KI-gestützte
# Wetter-Verschleiß-Schätzung für Komponenten", Abschnitt 1). Noch nicht aus
# echten Nutzerdaten kalibriert (last_calibrated_at bleibt NULL).
DEFAULTS = {
    "drivetrain": dict(rain=0.90, heat=0.05, cold=0.05, wind=0.10),
    "brakes": dict(rain=0.55, heat=0.05, cold=0.10, wind=0.05),
    "wheels": dict(rain=0.35, heat=0.55, cold=0.45, wind=0.05),
    "suspension": dict(rain=0.75, heat=0.30, cold=0.25, wind=0.05),
    "cockpit": dict(rain=0.25, heat=0.10, cold=0.10, wind=0.00),
    "frame": dict(rain=0.00, heat=0.00, cold=0.00, wind=0.00),
    "electric": dict(rain=0.15, heat=0.50, cold=0.40, wind=0.00),
    "lighting": dict(rain=0.00, heat=0.00, cold=0.00, wind=0.00),
    "other": dict(rain=0.00, heat=0.00, cold=0.00, wind=0.00),
}


def seed_coefficients(apps, schema_editor):
    WeatherSensitivityCoefficient = apps.get_model(
        "app_maintenance", "WeatherSensitivityCoefficient"
    )
    for category, c in DEFAULTS.items():
        WeatherSensitivityCoefficient.objects.update_or_create(
            category=category,
            defaults={
                "rain_sensitivity": c["rain"],
                "heat_sensitivity": c["heat"],
                "cold_sensitivity": c["cold"],
                "wind_sensitivity": c["wind"],
                "notes": "Heuristischer Startwert (siehe Implementierungsplan), noch nicht aus Nutzerdaten kalibriert.",
            },
        )


def unseed_coefficients(apps, schema_editor):
    WeatherSensitivityCoefficient = apps.get_model(
        "app_maintenance", "WeatherSensitivityCoefficient"
    )
    WeatherSensitivityCoefficient.objects.filter(category__in=DEFAULTS.keys()).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("app_maintenance", "0005_component_weather_wear_fields_and_sensitivity_model"),
    ]

    operations = [
        migrations.RunPython(seed_coefficients, unseed_coefficients),
    ]
