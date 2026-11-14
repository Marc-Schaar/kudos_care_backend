"""
Template-Filter fuer die E-Mail-Templates.

Ohne diese Filter druckten die Templates die rohen Status-Slugs aus
(`{{ w.wear.warn_status_overall }}` -> "critical") — in einer deutschsprachigen
E-Mail an Endnutzer schlicht falsch.
"""

from django import template

register = template.Library()

WARN_LABELS = {
    "ok": "In Ordnung",
    "warn": "Bald fällig",
    "critical": "Überfällig",
    "unknown": "Unbekannt",
}


@register.filter
def warn_label(status: str) -> str:
    """'critical' -> 'Überfällig'. Unbekannte Werte bleiben unveraendert stehen."""
    return WARN_LABELS.get(status, status)
