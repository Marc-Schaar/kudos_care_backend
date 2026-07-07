import json
from pathlib import Path

from django.core.management.color import no_style
from django.db import migrations

FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent / "fixtures" / "component_templates.json"
)


def load_fixture(apps, schema_editor):
    """
    Lädt die Fixture manuell über das historische Model (statt via
    `loaddata`, das immer das aktuelle/lebende Model verwendet). So bleibt
    diese Migration unabhängig davon funktionsfähig, welche Felder dem
    ComponentTemplate-Model in späteren Migrationen noch hinzugefügt werden.
    """
    ComponentTemplate = apps.get_model("app_maintenance", "ComponentTemplate")
    connection = schema_editor.connection
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        entries = json.load(f)

    for entry in entries:
        ComponentTemplate.objects.using(connection.alias).update_or_create(
            pk=entry["pk"], defaults=entry["fields"]
        )

    # Explizite PKs umgehen die DB-Sequenz — ohne Reset würde die nächste
    # auto-increment-Insert (z.B. eine neue eigene Vorlage) mit einer bereits
    # vergebenen ID kollidieren.
    with connection.cursor() as cursor:
        for statement in connection.ops.sequence_reset_sql(no_style(), [ComponentTemplate]):
            cursor.execute(statement)


def unload_fixture(apps, schema_editor):
    ComponentTemplate = apps.get_model("app_maintenance", "ComponentTemplate")
    ComponentTemplate.objects.filter(is_system=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("app_maintenance", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(load_fixture, unload_fixture),
    ]
