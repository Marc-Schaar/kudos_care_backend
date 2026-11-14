"""
Erster von drei Schritten auf dem Weg von Tacho-Schnappschüssen zu Nutzungszeiträumen
(siehe `api/usage.py`): legt die neuen Felder an, ohne die alten anzufassen. `0025`
überträgt die Daten, `0026` entfernt, was danach übrig ist.

Getrennt, weil Postgres Schema-Änderungen und Daten-Änderungen an derselben Tabelle in
einer Transaktion nicht zuverlässig mischt ("pending trigger events").
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("app_auth", "__first__"),
        ("app_maintenance", "0023_group_kind_and_mount_position"),
    ]

    operations = [
        migrations.AlterField(
            model_name="bike",
            name="strava_bike_id",
            field=models.CharField(blank=True, max_length=50, null=True, unique=True),
        ),
        migrations.AddField(
            model_name="componenttemplate",
            name="owner",
            field=models.ForeignKey(
                blank=True,
                help_text="Besitzer einer eigenen Vorlage. Leer = gemeinsamer Katalog.",
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="custom_templates",
                to="app_auth.stravaprofile",
            ),
        ),
        migrations.AddField(
            model_name="componenttemplate",
            name="follows_wheel",
            field=models.BooleanField(
                default=False,
                help_text="Sitzt nicht auf dem Laufrad, muss aber dazu passen (Bremsbelaege, Bremsscheiben). Der Client schlaegt diese Teile als Kopplung vor, wenn ein zweites Laufrad angelegt wird. Siehe BikeAssembly.linked_components.",
            ),
        ),
        # ── Komponente ───────────────────────────────────────────────────────
        migrations.AddField(
            model_name="component",
            name="status",
            field=models.CharField(
                choices=[
                    ("mounted", "Montiert"),
                    ("spare", "Ersatz"),
                    ("retired", "Ausgemustert"),
                ],
                db_index=True,
                default="mounted",
                max_length=10,
            ),
        ),
        migrations.RenameField(
            model_name="component",
            old_name="carried_over_wear_km",
            new_name="prior_km",
        ),
        migrations.RenameField(
            model_name="component",
            old_name="carried_over_weather_wear_km",
            new_name="prior_weather_km",
        ),
        migrations.CreateModel(
            name="ComponentUsagePeriod",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "started_at",
                    models.DateTimeField(help_text="Ab hier zählen die Fahrten."),
                ),
                (
                    "ended_at",
                    models.DateTimeField(
                        blank=True,
                        help_text="Fahrten ab hier zählen nicht mehr. None = läuft noch.",
                        null=True,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "component",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="periods",
                        to="app_maintenance.component",
                    ),
                ),
            ],
            options={
                "verbose_name": "Komponenten-Nutzungszeitraum",
                "verbose_name_plural": "Komponenten-Nutzungszeiträume",
                "ordering": ["started_at", "id"],
            },
        ),
        # ── Baugruppe ────────────────────────────────────────────────────────
        migrations.AddField(
            model_name="bikeassembly",
            name="linked_assemblies",
            field=models.ManyToManyField(
                blank=True,
                help_text="Baugruppen, die zusammengehören (Vorder- und Hinterrad eines Satzes).",
                to="app_maintenance.bikeassembly",
            ),
        ),
        migrations.AddField(
            model_name="bikeassembly",
            name="linked_components",
            field=models.ManyToManyField(
                blank=True,
                help_text="Einzelne Teile aus anderen Bereichen, die beim Aufziehen dieser Baugruppe mit montiert werden (Bremsbelaege zum Carbon-Laufrad).",
                related_name="linked_to_assemblies",
                to="app_maintenance.component",
            ),
        ),
        migrations.AddField(
            model_name="assemblyusageperiod",
            name="started_moment",
            field=models.DateTimeField(null=True),
        ),
        migrations.AddField(
            model_name="assemblyusageperiod",
            name="ended_moment",
            field=models.DateTimeField(null=True, blank=True),
        ),
        # ── Baselines ────────────────────────────────────────────────────────
        migrations.AddField(
            model_name="componentcheck",
            name="baseline_at",
            field=models.DateTimeField(
                blank=True,
                help_text="Ab hier zählen die km für den Warn-Status neu (Uhr der Fahrten). Leer = ab dem Morgen von checked_at.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="maintenanceinterval",
            name="km_baseline_at",
            field=models.DateTimeField(
                blank=True,
                help_text="Ab hier zählen die km seit der letzten Erledigung (Uhr der Fahrten). Leer = ab dem Morgen von last_done_at.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="maintenancelog",
            name="previous_done_at",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="maintenancelog",
            name="previous_baseline_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
