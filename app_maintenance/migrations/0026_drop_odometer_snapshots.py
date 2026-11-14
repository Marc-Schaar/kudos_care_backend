"""
Letzter Schritt: entfernt die Tacho-Schnappschüsse und macht die Zeiträume zu den
einzigen Feldern, an denen die km hängen (siehe `0025` und `api/usage.py`).
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("app_maintenance", "0025_backfill_usage_periods"),
    ]

    operations = [
        # ── Baugruppen-Perioden: von Datum + km zu Zeitpunkt ─────────────────
        # Der Index hängt an `ended_at` und verschwände mit der Spalte, ohne dass der
        # Migrations-Zustand das mitbekommt.
        migrations.RemoveIndex(model_name="assemblyusageperiod", name="app_mainten_assembl_4d1e11_idx"),
        migrations.RemoveField(
            model_name="assemblyusageperiod", name="started_at"
        ),
        migrations.RemoveField(
            model_name="assemblyusageperiod", name="ended_at"
        ),
        migrations.RemoveField(
            model_name="assemblyusageperiod", name="started_distance_km"
        ),
        migrations.RemoveField(
            model_name="assemblyusageperiod", name="ended_distance_km"
        ),
        migrations.RenameField(
            model_name="assemblyusageperiod",
            old_name="started_moment",
            new_name="started_at",
        ),
        migrations.RenameField(
            model_name="assemblyusageperiod",
            old_name="ended_moment",
            new_name="ended_at",
        ),
        migrations.AlterField(
            model_name="assemblyusageperiod",
            name="started_at",
            field=models.DateTimeField(help_text="Zeitpunkt der Montage."),
        ),
        migrations.AlterField(
            model_name="assemblyusageperiod",
            name="ended_at",
            field=models.DateTimeField(
                blank=True,
                help_text="Zeitpunkt des Abziehens. None = aktuell montiert.",
                null=True,
            ),
        ),
        migrations.AddIndex(
            model_name="assemblyusageperiod",
            index=models.Index(
                fields=["assembly", "ended_at"], name="app_mainten_assembl_4d1e11_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="assemblyusageperiod",
            constraint=models.UniqueConstraint(
                condition=models.Q(("ended_at__isnull", True)),
                fields=("assembly",),
                name="uniq_open_period_per_assembly",
            ),
        ),
        # ── Komponenten ──────────────────────────────────────────────────────
        migrations.RemoveField(model_name="component", name="is_mounted"),
        migrations.RemoveField(model_name="component", name="distance_at_install"),
        migrations.RemoveField(model_name="component", name="distance_at_retire"),
        migrations.AlterModelOptions(
            name="component", options={"ordering": ["-installed_at", "-id"]}
        ),
        migrations.AddConstraint(
            model_name="component",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status", "mounted")),
                fields=("slot",),
                name="uniq_mounted_component_per_slot",
            ),
        ),
        migrations.AddIndex(
            model_name="componentusageperiod",
            index=models.Index(
                fields=["component", "ended_at"], name="app_mainten_compone_5ba360_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="componentusageperiod",
            constraint=models.UniqueConstraint(
                condition=models.Q(("ended_at__isnull", True)),
                fields=("component",),
                name="uniq_open_period_per_component",
            ),
        ),
        # ── Baselines ────────────────────────────────────────────────────────
        migrations.RemoveField(
            model_name="componentcheck", name="checked_at_distance_km"
        ),
        migrations.RemoveField(
            model_name="maintenanceinterval", name="last_done_distance_km"
        ),
        migrations.RemoveField(model_name="maintenancelog", name="done_distance_km"),
        # ── Nur Hilfetexte ───────────────────────────────────────────────────
        migrations.AlterField(
            model_name="component",
            name="installed_at",
            field=models.DateField(
                blank=True,
                help_text="Tag des ersten Einbaus. Bleibt bei späteren Wechseln stehen, denn daran hängt die Tage-Achse: Gummi altert auch, während das Teil im Regal liegt.",
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="component",
            name="prior_km",
            field=models.FloatField(
                blank=True,
                help_text="Km, die das Teil schon hatte, bevor es hier erfasst wurde (gebraucht gekauft, vor der App gefahren). Wird auf die gefahrenen km addiert.",
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="component",
            name="prior_weather_km",
            field=models.FloatField(
                blank=True,
                help_text="Analog zu prior_km für die wetter-gewichtete Achse.",
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="component",
            name="retired_at",
            field=models.DateField(
                blank=True,
                help_text="Tag des Ausmusterns. Nur bei status=retired gesetzt.",
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="componentcheck",
            name="checked_at_weather_wear_km",
            field=models.FloatField(
                blank=True,
                help_text="component.weather_wear_km zum Zeitpunkt der Prüfung — Baseline, damit der wetterbereinigte Warn-Status nach einer Freigabe ebenfalls zurückgesetzt wird (analog zu baseline_at für die km-Achse).",
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="componentcheck",
            name="snooze_km",
            field=models.FloatField(
                blank=True,
                help_text="Erneut warnen nach X weiteren km ab der Prüfung",
                null=True,
            ),
        ),
    ]
