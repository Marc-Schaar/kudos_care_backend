import django.db.models.deletion
from django.db import migrations, models

# Initiale Baugruppen fuer den Quick-Change-Flow. Verweist auf feste PKs aus
# fixtures/component_templates.json (siehe CLAUDE.md/Plan fuer die Herleitung).
GROUPS = {
    "Laufrad vorne": [19, 21, 23, 25, 27, 30, 58],
    "Laufrad hinten": [20, 22, 24, 26, 28, 29, 31, 59],
}


def seed_groups(apps, schema_editor):
    ComponentGroup = apps.get_model("app_maintenance", "ComponentGroup")
    ComponentTemplate = apps.get_model("app_maintenance", "ComponentTemplate")
    for name, template_pks in GROUPS.items():
        group, _ = ComponentGroup.objects.get_or_create(name=name)
        ComponentTemplate.objects.filter(pk__in=template_pks).update(group=group)


def unseed_groups(apps, schema_editor):
    ComponentGroup = apps.get_model("app_maintenance", "ComponentGroup")
    ComponentGroup.objects.filter(name__in=GROUPS.keys()).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("app_maintenance", "0013_bike_predicted_unsafe_notified_for_date_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="ComponentGroup",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=100)),
                ("notes", models.TextField(blank=True)),
            ],
            options={
                "ordering": ["name"],
            },
        ),
        migrations.AddField(
            model_name="componenttemplate",
            name="group",
            field=models.ForeignKey(
                blank=True,
                help_text="Optionale Baugruppe für den Quick-Change (z.B. 'Laufrad vorne').",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="templates",
                to="app_maintenance.componentgroup",
            ),
        ),
        migrations.RunPython(seed_groups, unseed_groups),
    ]
