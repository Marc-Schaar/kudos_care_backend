from django.db import migrations

# Kassette (Fixture-PK 2) gehörte bisher zur Baugruppe "Antrieb". Physisch sitzt
# sie aber auf dem Freilaufkörper des Hinterrads (Template-PK 29, bereits Teil
# von "Laufrad hinten") und wird beim Laufradwechsel typischerweise mitgetauscht
# — daher soll sie zu "Laufrad hinten" gehören, damit `assemblies/<id>/swap/`
# bzw. `activate/` sie mit erfasst.
TARGET_GROUP_NAME = "Laufrad hinten"
CASSETTE_TEMPLATE_PK = 2


def move_forward(apps, schema_editor):
    ComponentTemplate = apps.get_model("app_maintenance", "ComponentTemplate")
    ComponentGroup = apps.get_model("app_maintenance", "ComponentGroup")
    ComponentSlot = apps.get_model("app_maintenance", "ComponentSlot")
    BikeAssembly = apps.get_model("app_maintenance", "BikeAssembly")
    AssemblyUsagePeriod = apps.get_model("app_maintenance", "AssemblyUsagePeriod")

    try:
        target_group = ComponentGroup.objects.get(name=TARGET_GROUP_NAME)
    except ComponentGroup.DoesNotExist:
        # Katalog-Seed (0016) ist auf dieser DB nie gelaufen — nichts zu tun,
        # der Fixture-Import selbst legt Kassette bereits gruppenlos an.
        return

    ComponentTemplate.objects.filter(pk=CASSETTE_TEMPLATE_PK).update(group=target_group)

    # Bestehende Kassette-Slots, die noch an ihrer alten Gruppe (Antrieb) hängen,
    # in den "Laufrad hinten"-Satz desselben Bikes umhängen — sofern dort einer
    # aktiv ist. Ohne aktiven Laufrad-hinten-Satz wird der Slot ungruppiert
    # (statt eine Baugruppe zu erzwingen); er taucht dann unter "Ohne
    # Baugruppe" auf und lässt sich per "vorhandene Komponente übernehmen"
    # manuell einsortieren.
    slots = (
        ComponentSlot.objects.filter(
            template_id=CASSETTE_TEMPLATE_PK, assembly__isnull=False
        )
        .exclude(assembly__group=target_group)
        .select_related("bike")
        .prefetch_related("components")
    )
    for slot in slots:
        rear_wheel = BikeAssembly.objects.filter(
            bike=slot.bike, group=target_group, is_active=True
        ).first()
        if rear_wheel is None:
            slot.assembly = None
            slot.save(update_fields=["assembly"])
            continue

        slot.assembly = rear_wheel
        slot.save(update_fields=["assembly"])

        mounted = next((c for c in slot.components.all() if c.is_mounted), None)
        if mounted is None:
            continue

        if mounted.installed_at and (
            rear_wheel.installed_at is None
            or mounted.installed_at < rear_wheel.installed_at
        ):
            rear_wheel.installed_at = mounted.installed_at
            rear_wheel.save(update_fields=["installed_at"])

        # Läuft der Laufrad-hinten-Satz bereits über eine Nutzungsperiode und
        # ist die Kassette älter als deren Beginn, den Beginn zurückdatieren —
        # sonst würde ihr bisheriger Verlauf beim nächsten Recompute
        # abgeschnitten (siehe api/usage.py).
        period = AssemblyUsagePeriod.objects.filter(
            assembly=rear_wheel, ended_at__isnull=True
        ).first()
        if period is None or not mounted.installed_at:
            continue
        update_fields = []
        if period.started_at is None or mounted.installed_at < period.started_at:
            period.started_at = mounted.installed_at
            update_fields.append("started_at")
        if mounted.distance_at_install is not None and (
            period.started_distance_km is None
            or mounted.distance_at_install < period.started_distance_km
        ):
            period.started_distance_km = mounted.distance_at_install
            update_fields.append("started_distance_km")
        if update_fields:
            period.save(update_fields=update_fields)


def move_backward(apps, schema_editor):
    """
    Grob umkehrbar (analog 0017/0019): Template-Zuordnung zurück auf "Antrieb",
    bereits verschobene Slots bleiben bei "Laufrad hinten" — ein vollständiges
    Zurückrechnen der Perioden-Anpassung wäre nicht mehr eindeutig rekonstruierbar.
    """
    ComponentTemplate = apps.get_model("app_maintenance", "ComponentTemplate")
    ComponentGroup = apps.get_model("app_maintenance", "ComponentGroup")

    try:
        source_group = ComponentGroup.objects.get(name="Antrieb")
    except ComponentGroup.DoesNotExist:
        return
    ComponentTemplate.objects.filter(pk=CASSETTE_TEMPLATE_PK).update(group=source_group)


class Migration(migrations.Migration):

    dependencies = [
        ("app_maintenance", "0019_backfill_assembly_usage_periods"),
    ]

    operations = [
        migrations.RunPython(move_forward, move_backward),
    ]
