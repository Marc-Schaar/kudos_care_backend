from django.db import migrations

# Vollständiger Baugruppen-Katalog. Template-PKs stammen fest aus
# fixtures/component_templates.json (pk 1-63, siehe 0002/0011). Muster wie 0004/0014:
# reine Daten-Migration über das historische Model, keine Schema-Änderung.
#
#   name: (category, sort_order, recommended, applicable_bike_types, [template_pks])
#
# "Laufrad vorne"/"Laufrad hinten" existieren bereits aus 0014 und werden per
# get_or_create wiederverwendet + um die neuen Felder ergänzt.
GROUPS = {
    "Antrieb": (
        "drivetrain",
        10,
        True,
        [],
        [1, 2, 3, 4, 5, 6, 7, 8, 9, 54, 55, 56, 57],
    ),
    "Bremse vorne": ("brakes", 20, True, [], [10, 12, 15, 17, 14]),
    "Bremse hinten": ("brakes", 21, True, [], [11, 13, 16, 18]),
    "Laufrad vorne": ("wheels", 30, True, [], [19, 21, 23, 25, 27, 30, 58]),
    "Laufrad hinten": ("wheels", 31, True, [], [20, 22, 24, 26, 28, 29, 31, 59]),
    "Federung": ("suspension", 40, True, ["mtb", "ebike_mtb"], [32, 33, 34, 35, 36]),
    "Cockpit": ("cockpit", 50, True, [], [37, 38, 39, 40, 41, 42, 43, 44]),
    "Rahmen & Lager": ("frame", 60, True, [], [45, 46, 47]),
    "E-Antrieb": (
        "electric",
        70,
        True,
        ["ebike_road", "ebike_mtb", "ebike_city"],
        [48, 49, 50, 51],
    ),
    "Beleuchtung": ("lighting", 80, False, ["city", "ebike_city", "other"], [52, 53]),
    "Zubehör": ("accessories", 90, False, [], [60, 61, 62, 63]),
}

# maintenance_kind='consumable' — Verbrauchsmaterial/Pflege ohne "Zustand",
# wird als MaintenanceInterval getrackt statt als Component.
CONSUMABLE_PKS = [14, 23, 24, 54, 55, 56, 57]

# Im Baugruppen-Dialog NICHT vorausgewählt (Nischen-/Alternativteile).
NON_DEFAULT_PKS = [4, 7, 8, 9, 17, 18, 29, 42, 44, 46, 50, 51, 54, 55, 57]


def seed(apps, schema_editor):
    ComponentGroup = apps.get_model("app_maintenance", "ComponentGroup")
    ComponentTemplate = apps.get_model("app_maintenance", "ComponentTemplate")

    for name, (
        category,
        sort_order,
        recommended,
        bike_types,
        template_pks,
    ) in GROUPS.items():
        group, _ = ComponentGroup.objects.get_or_create(name=name)
        group.category = category
        group.sort_order = sort_order
        group.recommended = recommended
        group.applicable_bike_types = bike_types
        group.is_system = True
        group.save()
        ComponentTemplate.objects.filter(pk__in=template_pks).update(group=group)

    ComponentTemplate.objects.filter(pk__in=CONSUMABLE_PKS).update(
        maintenance_kind="consumable"
    )
    ComponentTemplate.objects.filter(pk__in=NON_DEFAULT_PKS).update(
        default_in_group=False
    )


def unseed(apps, schema_editor):
    ComponentGroup = apps.get_model("app_maintenance", "ComponentGroup")
    ComponentTemplate = apps.get_model("app_maintenance", "ComponentTemplate")

    ComponentTemplate.objects.update(maintenance_kind="part", default_in_group=True)
    # Von 0016 neu angelegte Gruppen entfernen; die aus 0014 bleiben bestehen.
    keep = {"Laufrad vorne", "Laufrad hinten"}
    ComponentTemplate.objects.filter(
        group__name__in=[n for n in GROUPS if n not in keep]
    ).update(group=None)
    ComponentGroup.objects.filter(
        name__in=[n for n in GROUPS if n not in keep]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("app_maintenance", "0015_assembly_models"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
