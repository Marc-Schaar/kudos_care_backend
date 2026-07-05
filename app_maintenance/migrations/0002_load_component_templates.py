from django.core.management import call_command
from django.db import migrations


def load_fixture(apps, schema_editor):
    call_command("loaddata", "component_templates")


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
