from django.db import migrations


def backfill(apps, schema_editor):
    """
    Ordnet jeden bestehenden ComponentSlot seiner Baugruppe zu (erzwungen — der
    Katalog aus 0016 hat jedes System-Template einer Gruppe zugewiesen):

    - Verschleißteil-Slots (maintenance_kind='part'): an eine BikeAssembly für
      (bike, group) hängen. installed_at der Baugruppe = frühestes installed_at
      der montierten Komponenten.
    - Verbrauchsmaterial-Slots (maintenance_kind='consumable'): in ein
      MaintenanceInterval überführen (Baseline aus der montierten Komponente),
      danach Slot + Komponenten löschen, damit nichts doppelt erscheint.
    - Slots ohne Gruppe (nur denkbar bei vom User selbst angelegten Templates)
      bleiben unangetastet (assembly=None).
    """
    ComponentSlot = apps.get_model("app_maintenance", "ComponentSlot")
    BikeAssembly = apps.get_model("app_maintenance", "BikeAssembly")
    MaintenanceInterval = apps.get_model("app_maintenance", "MaintenanceInterval")

    kind_map = {
        "Tubeless Dichtmilch vorne": "sealant",
        "Tubeless Dichtmilch hinten": "sealant",
        "Bremsflüssigkeit (DOT/Mineral)": "brake_bleed",
        "Kettenwachs (Heißwachs/Tauchbad) auffrischen": "chain_lube",
        "Kettenwachs (Tropf-/Flüssigwachs) auffrischen": "chain_lube",
        "Kettenöl / Kettenschmierung auffrischen": "chain_lube",
        "Schaltung Akku (Di2/AXS) laden": "di2_charge",
    }

    assemblies: dict[tuple[int, int], object] = {}

    slots = (
        ComponentSlot.objects.filter(
            assembly__isnull=True, template__group__isnull=False
        )
        .select_related("template", "template__group", "bike")
        .prefetch_related("components")
    )

    for slot in slots:
        template = slot.template
        group = template.group
        mounted = next((c for c in slot.components.all() if c.is_mounted), None)

        if template.maintenance_kind == "consumable":
            MaintenanceInterval.objects.create(
                bike=slot.bike,
                assembly=None,  # wird unten nachgezogen sobald die Assembly existiert
                template=template,
                kind=kind_map.get(template.name, "custom"),
                label=template.name,
                interval_km=template.warn_km,
                interval_days=template.warn_days,
                last_done_at=mounted.installed_at if mounted else None,
                last_done_distance_km=mounted.distance_at_install if mounted else None,
                notes=slot.custom_name or "",
            )
            slot.delete()
            continue

        key = (slot.bike_id, group.id)
        assembly = assemblies.get(key)
        if assembly is None:
            assembly, _ = BikeAssembly.objects.get_or_create(
                bike=slot.bike,
                group=group,
                is_active=True,
                defaults={"name": ""},
            )
            assemblies[key] = assembly
        slot.assembly = assembly
        slot.save(update_fields=["assembly"])

        if mounted and mounted.installed_at:
            if (
                assembly.installed_at is None
                or mounted.installed_at < assembly.installed_at
            ):
                assembly.installed_at = mounted.installed_at
                assembly.save(update_fields=["installed_at"])

    # Verbrauchsmaterial-Intervalle nachträglich ihrer (jetzt existierenden)
    # Baugruppe zuordnen, falls für dasselbe (bike, group) eine angelegt wurde.
    for interval in MaintenanceInterval.objects.filter(
        assembly__isnull=True, template__group__isnull=False
    ).select_related("template__group"):
        assembly = assemblies.get((interval.bike_id, interval.template.group_id))
        if assembly is not None:
            interval.assembly = assembly
            interval.save(update_fields=["assembly"])


def reverse(apps, schema_editor):
    """
    Grob umkehrbar: Zuordnung lösen und Assemblies/Intervalle entfernen. Die aus
    Verbrauchsmaterial-Slots gelöschten Components werden NICHT wiederhergestellt
    (nur Log-Hinweis über den Verlust).
    """
    ComponentSlot = apps.get_model("app_maintenance", "ComponentSlot")
    BikeAssembly = apps.get_model("app_maintenance", "BikeAssembly")
    MaintenanceInterval = apps.get_model("app_maintenance", "MaintenanceInterval")

    ComponentSlot.objects.filter(assembly__isnull=False).update(assembly=None)
    MaintenanceInterval.objects.all().delete()
    BikeAssembly.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("app_maintenance", "0016_seed_assembly_catalog"),
    ]

    operations = [
        migrations.RunPython(backfill, reverse),
    ]
