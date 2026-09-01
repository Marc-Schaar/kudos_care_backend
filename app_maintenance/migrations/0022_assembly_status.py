"""
Ersetzt die Zwei-Feld-Kodierung `is_active` + `retired_at` durch ein einzelnes
`status`-Feld (active/parked/retired) und macht die Invariante "höchstens eine
aufgezogene Instanz je (bike, group)" zu einem partiellen Unique-Constraint.

Reihenfolge ist wichtig: `status` wird **vor** dem Entfernen von `is_active`
befüllt, sonst wäre die Unterscheidung geparkt/ausgemustert verloren. Vor dem
Constraint werden etwaige Altbestände mit mehreren aktiven Instanzen je
(bike, group) aufgeräumt — `clean()` hat das nie DB-seitig garantiert, und
Daten aus Admin/Seed/Datenmigrationen könnten daran vorbeigelaufen sein.
"""

from django.db import migrations, models


def set_status_from_flags(apps, schema_editor):
    """
    is_active=True                      -> active
    is_active=False, retired_at IS NULL -> parked
    retired_at gesetzt                  -> retired

    `retired_at` gewinnt gegen `is_active`: eine Zeile mit beidem ist
    widersprüchlich, und "ausgemustert" ist die Aussage, die die Komponenten
    bereits ausgebaut hat.
    """
    BikeAssembly = apps.get_model("app_maintenance", "BikeAssembly")
    BikeAssembly.objects.filter(retired_at__isnull=False).update(status="retired")
    BikeAssembly.objects.filter(retired_at__isnull=True, is_active=True).update(
        status="active"
    )
    BikeAssembly.objects.filter(retired_at__isnull=True, is_active=False).update(
        status="parked"
    )


def restore_flags_from_status(apps, schema_editor):
    """Rückrichtung für `migrate app_maintenance 0021`."""
    BikeAssembly = apps.get_model("app_maintenance", "BikeAssembly")
    BikeAssembly.objects.filter(status="active").update(is_active=True)
    BikeAssembly.objects.exclude(status="active").update(is_active=False)


def park_surplus_active_assemblies(apps, schema_editor):
    """
    Sichert die Vorbedingung des Unique-Constraints: gibt es zu einem
    (bike, group) mehrere aktive Instanzen, bleibt die zuletzt angelegte
    aufgezogen, die übrigen werden geparkt (nicht ausgemustert — ihre
    Komponenten sitzen ja weiterhin montiert auf dem Satz).
    """
    BikeAssembly = apps.get_model("app_maintenance", "BikeAssembly")
    seen: set[tuple[int, int]] = set()
    surplus: list[int] = []
    for assembly in (
        BikeAssembly.objects.filter(status="active")
        .order_by("bike_id", "group_id", "-created_at", "-id")
        .only("id", "bike_id", "group_id")
    ):
        key = (assembly.bike_id, assembly.group_id)
        if key in seen:
            surplus.append(assembly.id)
        else:
            seen.add(key)
    if surplus:
        BikeAssembly.objects.filter(id__in=surplus).update(status="parked")


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("app_maintenance", "0021_component_carried_over_wear_km_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="bikeassembly",
            name="status",
            field=models.CharField(
                choices=[
                    ("active", "Aufgezogen"),
                    ("parked", "Geparkt"),
                    ("retired", "Ausgemustert"),
                ],
                db_index=True,
                default="active",
                max_length=10,
            ),
        ),
        migrations.RunPython(set_status_from_flags, restore_flags_from_status),
        migrations.RunPython(park_surplus_active_assemblies, noop),
        migrations.RemoveField(
            model_name="bikeassembly",
            name="is_active",
        ),
        migrations.AlterField(
            model_name="bikeassembly",
            name="retired_at",
            field=models.DateField(
                blank=True,
                help_text="Tag des Ausmusterns. Nur bei status=retired gesetzt.",
                null=True,
            ),
        ),
        migrations.AddConstraint(
            model_name="bikeassembly",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status", "active")),
                fields=("bike", "group"),
                name="uniq_active_assembly_per_bike_group",
            ),
        ),
    ]
