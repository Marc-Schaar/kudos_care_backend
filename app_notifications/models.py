from django.db import models  # noqa: F401

# Keine eigenen Models — Dedupe-/Status-Felder für Benachrichtigungen leben direkt an den
# betroffenen Domain-Models (StravaProfile, Component, Bike), analog zum bestehenden
# Cache-Feld-Muster im Projekt (siehe app_maintenance.models.Component.weather_wear_explanation).
