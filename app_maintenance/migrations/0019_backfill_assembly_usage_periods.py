"""
Backfill: für jede AKTIVE Baugruppe eine offene Nutzungsperiode anlegen.

Bis hierher gab es je (bike, group) nur eine Instanz, die ab Einbau lückenlos am
Rad war — die offene Periode bildet genau das ab. Ausgemusterte/inaktive
Altbestände bekommen bewusst KEINE Periode: für sie ist kein km-Stand des
Abziehens erfasst, und `api/usage.py` fällt ohne Perioden auf die frühere
Rechnung zurück. Ihre Zahlen bleiben damit unverändert.
"""

from django.db import migrations


def create_open_periods(apps, schema_editor):
    BikeAssembly = apps.get_model("app_maintenance", "BikeAssembly")
    AssemblyUsagePeriod = apps.get_model("app_maintenance", "AssemblyUsagePeriod")

    assemblies = BikeAssembly.objects.filter(is_active=True).prefetch_related(
        "slots__components"
    )
    for assembly in assemblies:
        if AssemblyUsagePeriod.objects.filter(assembly=assembly).exists():
            continue

        components = [
            c
            for slot in assembly.slots.all()
            for c in slot.components.all()
            if c.is_mounted
        ]
        install_dates = [c.installed_at for c in components if c.installed_at]
        install_km = [
            c.distance_at_install
            for c in components
            if c.distance_at_install is not None
        ]

        started_at = assembly.installed_at or (
            min(install_dates) if install_dates else None
        )
        if started_at is None:
            # Ohne Startdatum lässt sich kein sinnvoller Zeitraum bilden — der
            # Fallback in api/usage.py liefert weiter die bisherigen Werte.
            continue

        AssemblyUsagePeriod.objects.create(
            assembly=assembly,
            started_at=started_at,
            started_distance_km=min(install_km) if install_km else None,
        )


def drop_periods(apps, schema_editor):
    AssemblyUsagePeriod = apps.get_model("app_maintenance", "AssemblyUsagePeriod")
    AssemblyUsagePeriod.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("app_maintenance", "0018_component_distance_at_retire_assemblyusageperiod"),
    ]

    operations = [
        migrations.RunPython(create_open_periods, drop_periods),
    ]
