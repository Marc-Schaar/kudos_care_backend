from django.db import migrations

NON_ESTIMATABLE_TEMPLATE_NAMES = [
    "Tretlager",
    "Steuersatz",
    "Nabenlager vorne",
    "Nabenlager hinten",
    "Hinterbaulager",
    "Gabelöl / Lower Leg Service",
    "Gabel Full Service (Kartusche)",
    "Dämpfer Service (Basic)",
    "Dämpfer Full Service",
    "Federgabel Dichtungen",
    "Motor Service",
]


def set_supports_condition_estimate(apps, schema_editor):
    ComponentTemplate = apps.get_model("app_maintenance", "ComponentTemplate")
    ComponentTemplate.objects.filter(
        name__in=NON_ESTIMATABLE_TEMPLATE_NAMES
    ).update(supports_condition_estimate=False)


def reverse_supports_condition_estimate(apps, schema_editor):
    ComponentTemplate = apps.get_model("app_maintenance", "ComponentTemplate")
    ComponentTemplate.objects.filter(
        name__in=NON_ESTIMATABLE_TEMPLATE_NAMES
    ).update(supports_condition_estimate=True)


class Migration(migrations.Migration):

    dependencies = [
        ("app_maintenance", "0003_component_custom_warn_days_component_custom_warn_km_and_more"),
    ]

    operations = [
        migrations.RunPython(
            set_supports_condition_estimate, reverse_supports_condition_estimate
        ),
    ]
