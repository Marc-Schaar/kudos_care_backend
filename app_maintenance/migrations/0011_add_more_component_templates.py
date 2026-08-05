import json
from pathlib import Path

from django.core.management.color import no_style
from django.db import migrations

FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent / "fixtures" / "component_templates.json"
)

# Neu in dieser Migration ergänzte Vorlagen (Kettenwachs/-öl, E-Schaltungsakku,
# Felgenband, Zubehör) — siehe fixtures/component_templates.json pk 54-63.
NEW_TEMPLATE_PKS = list(range(54, 64))

ACCESSORIES_COEFFICIENT = dict(rain=0.00, heat=0.00, cold=0.00, wind=0.00)


def load_new_templates(apps, schema_editor):
    """
    Ergänzt nur die neuen Fixture-Einträge über das historische Model (analog
    0002_load_component_templates). Bereits bestehende PKs 1-53 werden hier
    nicht angefasst, damit lokale Anpassungen an alten Einträgen unberührt
    bleiben.
    """
    ComponentTemplate = apps.get_model("app_maintenance", "ComponentTemplate")
    WeatherSensitivityCoefficient = apps.get_model(
        "app_maintenance", "WeatherSensitivityCoefficient"
    )
    connection = schema_editor.connection

    with open(FIXTURE_PATH, encoding="utf-8") as f:
        entries = json.load(f)

    new_entries = [e for e in entries if e["pk"] in NEW_TEMPLATE_PKS]
    for entry in new_entries:
        ComponentTemplate.objects.using(connection.alias).update_or_create(
            pk=entry["pk"], defaults=entry["fields"]
        )

    with connection.cursor() as cursor:
        for statement in connection.ops.sequence_reset_sql(no_style(), [ComponentTemplate]):
            cursor.execute(statement)

    WeatherSensitivityCoefficient.objects.using(connection.alias).update_or_create(
        category="accessories",
        defaults={
            "rain_sensitivity": ACCESSORIES_COEFFICIENT["rain"],
            "heat_sensitivity": ACCESSORIES_COEFFICIENT["heat"],
            "cold_sensitivity": ACCESSORIES_COEFFICIENT["cold"],
            "wind_sensitivity": ACCESSORIES_COEFFICIENT["wind"],
            "notes": "Heuristischer Startwert (kein wetterabhängiger Verschleiß angenommen), noch nicht aus Nutzerdaten kalibriert.",
        },
    )


def unload_new_templates(apps, schema_editor):
    ComponentTemplate = apps.get_model("app_maintenance", "ComponentTemplate")
    WeatherSensitivityCoefficient = apps.get_model(
        "app_maintenance", "WeatherSensitivityCoefficient"
    )
    ComponentTemplate.objects.filter(pk__in=NEW_TEMPLATE_PKS).delete()
    WeatherSensitivityCoefficient.objects.filter(category="accessories").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("app_maintenance", "0010_accessories_category"),
    ]

    operations = [
        migrations.RunPython(load_new_templates, unload_new_templates),
    ]
