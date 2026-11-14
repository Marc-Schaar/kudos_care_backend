"""
Macht "vorne/hinten" und "am Stück wechselbar?" zu Feldern.

Beides war vorher implizit: die Seite steckte nur im Freitext-Namen (und wurde
im Frontend-Diagramm per String-Match wieder herausgelesen), und ob eine Gruppe
als Ganzes getauscht werden kann, stand nirgends — `activate`/`swap` wurden für
jede Gruppe angeboten, auch für "Cockpit".

Der Backfill leitet die Seite einmalig aus den heutigen Namen ab (die sind
aktuell konsistent) und setzt danach die Ausnahmen explizit. Wichtigster Fall:
die **Kassette** hat "hinten" nie im Namen getragen — sie sass nur wegen einer
Sonderregel im Diagramm hinten. Sie bekommt hier `rear` fest zugewiesen.
"""

from django.db import migrations, models

# Nur die Laufradsätze sind physische Einheiten, die man am Stück ab- und
# aufzieht (Sommer-/Winter-LRS). Alles andere teilt sich einen Ort, verschleißt
# aber unabhängig: Bremsbeläge 3.000 km neben Bremsscheibe 15.000 km, Kette
# 2.000 km neben Kurbel 30.000 km. Dort wird einzeln getauscht.
ASSEMBLY_GROUP_NAMES = ["Laufrad vorne", "Laufrad hinten"]

#: Templates, deren Name die Seite nicht verrät. Ohne diesen Eintrag bliebe die
#: Kassette ohne Position und wäre im Diagramm wieder Auslegungssache.
POSITION_BY_TEMPLATE_NAME = {
    "Kassette": "rear",
    "Freilaufkörper": "rear",
    "Schaltwerk": "rear",
    "Umwerfer": "front",
    "Hinterbaulager": "rear",
    "Umlenkrollen Hinterbau": "rear",
    "Frontlicht Akku": "front",
    "Rücklicht Akku": "rear",
}


def _position_from_name(name):
    """Leitet die Seite aus dem Namen ab — einmalig, danach zählt nur das Feld."""
    lowered = name.lower()
    if "vorne" in lowered or "front" in lowered:
        return "front"
    if "hinten" in lowered or "rück" in lowered:
        return "rear"
    return ""


def set_position_and_kind(apps, schema_editor):
    ComponentGroup = apps.get_model("app_maintenance", "ComponentGroup")
    ComponentTemplate = apps.get_model("app_maintenance", "ComponentTemplate")

    for group in ComponentGroup.objects.all():
        group.position = _position_from_name(group.name)
        group.kind = "assembly" if group.name in ASSEMBLY_GROUP_NAMES else "area"
        group.save(update_fields=["position", "kind"])

    for template in ComponentTemplate.objects.all():
        template.position = POSITION_BY_TEMPLATE_NAME.get(
            template.name, _position_from_name(template.name)
        )
        template.save(update_fields=["position"])


def clear_position_and_kind(apps, schema_editor):
    """Rückrichtung: die Felder verschwinden ohnehin, hier bleibt nichts zu tun."""


class Migration(migrations.Migration):

    dependencies = [
        ("app_maintenance", "0022_assembly_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="componentgroup",
            name="kind",
            field=models.CharField(
                choices=[
                    ("assembly", "Baugruppe (am Stück wechselbar)"),
                    ("area", "Bereich (Teile einzeln)"),
                ],
                default="area",
                help_text="ASSEMBLY = am Stück wechselbar (Laufradsatz), AREA = Teile verschleißen unabhängig und werden einzeln getauscht. Steuert, ob activate/swap überhaupt angeboten werden. Siehe GroupKind.",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="componentgroup",
            name="position",
            field=models.CharField(
                blank=True,
                choices=[("front", "Vorne"), ("rear", "Hinten")],
                default="",
                help_text="Vorne/hinten als Feld statt als Wort im Namen. Leer = ohne Seite (Antrieb, Cockpit, ...). Siehe MountPosition.",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="componenttemplate",
            name="position",
            field=models.CharField(
                blank=True,
                choices=[("front", "Vorne"), ("rear", "Hinten")],
                default="",
                help_text="Vorne/hinten als Feld statt als Wort im Namen — das Diagramm liest das hier statt den Anzeigenamen zu durchsuchen, sodass Umbenennen die Position nicht mehr kaputtmacht. Leer = ohne Seite. Siehe MountPosition.",
                max_length=10,
            ),
        ),
        migrations.RunPython(set_position_and_kind, clear_position_and_kind),
    ]
