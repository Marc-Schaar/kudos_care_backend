"""
Führt ungruppierte Alt-Plätze mit ihrem Zwilling in einem Bereich zusammen.

Ein Bereich ("Bremse hinten") hat seit `0025` genau eine Instanz, und mehrere Beläge
liegen als mehrere Teile im selben Platz. Übrig blieben ungruppierte Plätze mit
demselben Template, die schon beim Backfill in `0017` nicht in den Bereich passten
(`uniq_assembly_template`). Die Folge in Produktion: zweimal "Bremsbeläge Felge
hinten" in der Kopplungs-Auswahl, und der Belag im Bereich fand kein Ziel zum
Umziehen, weil der Zwilling in keiner Baugruppe hing.

Die Teile ziehen in den Platz des Bereichs um, der leere Alt-Platz entfällt. Die km
hängen an den Zeiträumen der Teile, Kopplungen an den Teilen selbst — beides bleibt
unberührt. Sind in beiden Plätzen Teile montiert, wäre nach dem Umzug ein Platz
doppelt belegt; diesen Fall lässt die Migration stehen statt einen Belag abzunehmen.
"""

from django.db import migrations


def merge_ungrouped_area_twins(apps, schema_editor):
    Slot = apps.get_model("app_maintenance", "ComponentSlot")
    Component = apps.get_model("app_maintenance", "Component")

    ungrouped = Slot.objects.filter(assembly__isnull=True).select_related("template")
    for slot in ungrouped:
        target = (
            Slot.objects.filter(
                bike_id=slot.bike_id,
                template_id=slot.template_id,
                assembly__group__kind="area",
            )
            .exclude(assembly__status="retired")
            .first()
        )
        if target is None:
            continue

        both_mounted = (
            Component.objects.filter(slot=slot, status="mounted").exists()
            and Component.objects.filter(slot=target, status="mounted").exists()
        )
        if both_mounted:
            continue

        Component.objects.filter(slot=slot).update(slot=target)
        slot.delete()


class Migration(migrations.Migration):

    dependencies = [
        ("app_maintenance", "0026_drop_odometer_snapshots"),
    ]

    operations = [
        migrations.RunPython(merge_ungrouped_area_twins, migrations.RunPython.noop),
    ]
