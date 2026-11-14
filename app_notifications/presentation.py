"""
Aufbereitung der Verschleißdaten für die Mails.

Die Templates sollen nichts rechnen: alles, was eine Zeile zeigt (Zahl, nach der man
handelt, Herleitung, Balkenbreite, Statusform), entsteht hier. Die Regel für "noch
709 km" / "863 km drüber" ist bewusst dieselbe wie `partRemaining()` im Frontend
(`app-maintenance/shared/utils/utils.ts`) — App und Mail müssen dieselbe Zahl nennen,
sonst glaubt man keiner von beiden. Ändert sich die Regel dort, hier nachziehen.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

# Form + Farbe wie in der App (`.g-dot`): Kreis ok, Dreieck warn, Raute critical,
# Ring unbekannt. In Mails gibt es kein verlässliches SVG, daher Zeichen.
STATUS_SHAPES = {"critical": "◆", "warn": "▲", "ok": "●", "unknown": "○"}
_STATUS_ORDER = {"critical": 0, "warn": 1, "ok": 2, "unknown": 3}

_WEEKDAYS = [
    "Montag",
    "Dienstag",
    "Mittwoch",
    "Donnerstag",
    "Freitag",
    "Samstag",
    "Sonntag",
]
_MONTHS = [
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]  # fmt: skip


def format_count(value: float) -> str:
    """1063.4 -> '1.063'. Unabhängig von LANGUAGE_CODE, das im Projekt en-us ist."""
    return f"{round(value):,}".replace(",", ".")


def format_day(value: date) -> str:
    """date(2026, 11, 21) -> 'Samstag, 21. November'."""
    return f"{_WEEKDAYS[value.weekday()]}, {value.day}. {_MONTHS[value.month - 1]}"


def _days(count: int) -> str:
    return f"{format_count(count)} {'Tag' if count == 1 else 'Tage'}"


def _distance_to_limit(used: float, limit: float, unit: str) -> str:
    left = round(limit - used)
    label = (lambda n: f"{format_count(n)} km") if unit == "km" else _days
    if left == 0:
        return "jetzt fällig"
    return f"noch {label(left)}" if left > 0 else f"{label(-left)} drüber"


@dataclass(frozen=True)
class WearLine:
    """Eine Zeile im Werkstattzettel: ein Teil, wie weit es ist, und warum."""

    name: str
    part: str
    status: str
    remaining: str
    detail: str
    ratio: float | None

    @property
    def shape(self) -> str:
        return STATUS_SHAPES.get(self.status, STATUS_SHAPES["unknown"])

    @property
    def bar_pct(self) -> int | None:
        """Gefüllter Anteil des Balkens. Über der Grenze ist er voll, die Zahl erzählt den Rest."""
        if self.ratio is None:
            return None
        return max(3, min(100, round(self.ratio * 100)))

    @property
    def rest_pct(self) -> int:
        return 100 - (self.bar_pct or 0)


def wear_line(name: str, component: Any, wear: dict) -> WearLine:
    """
    Baut die Zeile aus dem Ergebnis von `compute_wear()`. Maßgeblich ist die Achse, die am
    weitesten ist; km zählen inklusive Wetteraufschlag, wie im Frontend.
    """
    wear_km = wear.get("wear_km") or 0.0
    weather_km = getattr(component, "weather_wear_km", None) or 0.0
    used_km = max(wear_km, weather_km)
    limit_km = getattr(component, "effective_warn_km", None)
    limit_days = getattr(component, "effective_warn_days", None)
    wear_days = wear.get("wear_days")

    ratio_km = used_km / limit_km if limit_km else None
    ratio_days = (
        wear_days / limit_days if limit_days and wear_days is not None else None
    )

    if ratio_km is not None and (ratio_days is None or ratio_km >= ratio_days):
        remaining = _distance_to_limit(used_km, limit_km, "km")
        detail = f"{format_count(used_km)} von {format_count(limit_km)} km"
        # Unter 1 % wäre der Aufschlag Rundungsrauschen (wie `weatherSurplusKm()`).
        if weather_km - wear_km >= wear_km * 0.01 and weather_km > wear_km:
            detail += " mit Wetteraufschlag"
        ratio = ratio_km
    elif ratio_days is not None:
        remaining = _distance_to_limit(wear_days, limit_days, "days")
        detail = f"{format_count(wear_days)} von {format_count(limit_days)} Tagen"
        ratio = ratio_days
    else:
        remaining = ""
        detail = f"{format_count(wear_km)} km" if wear_km else ""
        ratio = None

    return WearLine(
        name=name,
        part=(getattr(component, "label", "") or "").strip(),
        status=wear.get("warn_status_overall", "unknown"),
        remaining=remaining,
        detail=detail,
        ratio=ratio,
    )


@dataclass
class BikeGroup:
    bike: Any
    lines: list[WearLine] = field(default_factory=list)
    predicted_day: str = ""


def _sorted(lines: list[WearLine]) -> list[WearLine]:
    return sorted(
        lines, key=lambda line: (_STATUS_ORDER.get(line.status, 9), -(line.ratio or 0))
    )


def group_warnings(warnings: Iterable[dict]) -> list[BikeGroup]:
    """Die Einträge aus `get_new_component_warnings()` je Rad, das Dringendste zuerst."""
    groups: dict[int, BikeGroup] = {}
    for item in warnings:
        bike = item["bike"]
        group = groups.setdefault(bike.id, BikeGroup(bike=bike))
        group.lines.append(
            wear_line(item["slot"].display_name, item["component"], item["wear"])
        )
    for group in groups.values():
        group.lines = _sorted(group.lines)
    return list(groups.values())


def group_predictions(predictions: Iterable[dict]) -> list[BikeGroup]:
    """Die Einträge aus `get_predicted_unsafe_bikes()`, Zahlen auf den Fahrttag projiziert."""
    return [
        BikeGroup(
            bike=p["bike"],
            predicted_day=format_day(p["predicted_date"]),
            lines=_sorted(
                [
                    wear_line(c["slot"].display_name, c["component"], c["wear"])
                    for c in p["components"]
                ]
            ),
        )
        for p in predictions
    ]


def _parts(count: int) -> str:
    return "1 Teil" if count == 1 else f"{count} Teile"


def warning_headline(groups: list[BikeGroup]) -> str:
    """'2 Teile überfällig, 1 bald fällig' — der Befund als Überschrift."""
    lines = [line for group in groups for line in group.lines]
    critical = sum(line.status == "critical" for line in lines)
    warn = sum(line.status == "warn" for line in lines)
    if critical and warn:
        return f"{_parts(critical)} überfällig, {warn} bald fällig"
    if critical:
        return f"{_parts(critical)} überfällig"
    return f"{_parts(warn)} bald fällig"


def warning_subject(groups: list[BikeGroup]) -> str:
    """Der Betreff nennt das dringendste Teil, damit man ihn ohne Öffnen versteht."""
    lines = _sorted([line for group in groups for line in group.lines])
    first = lines[0]
    if len(lines) == 1:
        return f"{first.name}: {first.remaining}" if first.remaining else first.name
    others = len(lines) - 1
    rest = "1 weiteres Teil" if others == 1 else f"{others} weitere Teile"
    return f"{first.name} und {rest} fällig"


def prediction_subject(groups: list[BikeGroup]) -> str:
    lines = [line for group in groups for line in group.lines]
    if len(lines) == 1:
        return f"Vor der nächsten Fahrt: {lines[0].name} prüfen"
    return f"Vor der nächsten Fahrt: {_parts(len(lines))} prüfen"
