"""
Nutzungsfenster von Baugruppen und Komponenten — die **einzige** Quelle dafür,
in welchen Abschnitten ein Teil tatsächlich am Rad war.

Hintergrund: seit mehrere Instanzen derselben `ComponentGroup` nebeneinander
existieren dürfen (Sommer-/Winter-LRS), stimmt die alte Annahme "ab Einbau
lückenlos montiert" nicht mehr. `wear_km` und `WeatherWearService` würden einem
abgezogenen Laufradsatz sonst die km und das Wetter der Fahrten anrechnen, die
das Bike auf dem anderen Satz gemacht hat.

Zwei Achsen, dieselben Perioden (`AssemblyUsagePeriod`):

* **km-Fenster** (`(start_km, end_km)` auf dem Bike-Odometer) → `wear_km`,
  `km_since_check`, `BikeAssembly.compute_km()`
* **Datums-Fenster** (`(start_date, end_date|None)`) → Ride-Filter im
  `WeatherWearService` und `ride_wear_breakdown`

Die **Tage-Achse des Verschleißes bleibt bewusst außen vor**: Gummi und
Dichtmilch altern auch im Keller, `wear_days` läuft daher über Parkzeiten hinweg
weiter (siehe `compute_wear`).

Fallback: Slots ohne Baugruppe (ungruppierte Alt-Slots) und Baugruppen ohne
Perioden (Altbestand vor Migration 0018) liefern genau ein Fenster ab Einbau —
also exakt das frühere Verhalten. Bestehende Zahlen ändern sich dadurch nicht.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models import BikeAssembly, Component


KmWindow = tuple[float, float]
DateWindow = tuple[date, date | None]


def _clip_km(
    windows: list[KmWindow], low: float | None, high: float | None
) -> list[KmWindow]:
    """
    Schneidet km-Fenster auf [low, high] zu. Fenster der Länge 0 bleiben erhalten
    (frisch montiertes Teil = 0 km, nicht "unbekannt"), invertierte fallen weg.
    """
    clipped: list[KmWindow] = []
    for start, end in windows:
        if low is not None:
            start = max(start, low)
        if high is not None:
            end = min(end, high)
        if end >= start:
            clipped.append((start, end))
    return clipped


def assembly_km_windows(
    assembly: "BikeAssembly", bike_total_km: float | None
) -> list[KmWindow]:
    """
    Odometer-Abschnitte, in denen die Baugruppe am Rad war. Leere Liste, wenn
    keine Perioden erfasst sind (Altbestand) — der Aufrufer fällt dann auf die
    frühere Rechnung zurück.
    """
    if bike_total_km is None:
        return []

    windows: list[KmWindow] = []
    for period in assembly.periods.all():
        if period.started_distance_km is None:
            continue
        end = (
            period.ended_distance_km
            if period.ended_at is not None and period.ended_distance_km is not None
            else bike_total_km
        )
        if end >= period.started_distance_km:
            windows.append((period.started_distance_km, end))
    return sorted(windows)


def component_km_windows(
    component: "Component", bike_total_km: float | None
) -> list[KmWindow]:
    """
    Odometer-Abschnitte, in denen genau diese Komponente gefahren wurde: die
    Perioden ihrer Baugruppe, zugeschnitten auf ihre eigene Einbau-/Ausbau-Spanne.
    """
    if bike_total_km is None or component.distance_at_install is None:
        return []

    low = component.distance_at_install
    high = component.distance_at_retire if not component.is_mounted else None
    if high is None and not component.is_mounted:
        # Ausgebaut, aber ohne erfassten km-Stand (Altbestand): bis heute rechnen,
        # wie bisher — besser eine leicht zu hohe Zahl als gar keine.
        high = bike_total_km

    assembly = component.slot.assembly
    if assembly is not None:
        windows = assembly_km_windows(assembly, bike_total_km)
        if windows:
            return _clip_km(windows, low, high)

    return _clip_km([(low, bike_total_km)], low, high)


def component_active_km(
    component: "Component",
    bike_total_km: float | None,
    since_km: float | None = None,
) -> float | None:
    """
    Gefahrene km der Komponente, Parkzeiten herausgerechnet.

    `since_km` ist ein Odometer-Stand als Untergrenze — gebraucht für die
    Baseline nach einer Freigabe (`ComponentCheck.checked_at_distance_km`),
    damit eine Parkphase nach der Prüfung ebenfalls nicht mitzählt.

    `None` heißt "unbekannt" (kein Einbau-km-Stand, kein Bike-Odometer) — eine
    Baseline, die alle Fenster wegschneidet, ergibt dagegen 0.0.
    """
    windows = component_km_windows(component, bike_total_km)
    if not windows:
        return None
    if since_km is not None:
        windows = _clip_km(windows, since_km, None)
    return round(sum(end - start for start, end in windows), 1)


def component_date_windows(component: "Component") -> list[DateWindow]:
    """
    Datums-Abschnitte, in denen die Komponente gefahren wurde — für den
    Ride-Filter. `None` als Ende heißt "bis heute offen".
    """
    if component.installed_at is None:
        return []

    low = component.installed_at
    high = component.retired_at if not component.is_mounted else None

    assembly = component.slot.assembly
    raw: list[DateWindow]
    if assembly is not None and assembly.periods.all():
        raw = [(p.started_at, p.ended_at) for p in assembly.periods.all()]
    else:
        raw = [(low, None)]

    windows: list[DateWindow] = []
    for start, end in raw:
        start = max(start, low)
        if high is not None:
            end = high if end is None else min(end, high)
        if end is None or end >= start:
            windows.append((start, end))
    return sorted(windows, key=lambda w: w[0])


def date_in_windows(windows: list[DateWindow], day: date) -> bool:
    """War die Komponente an diesem Tag am Rad?"""
    return any(start <= day and (end is None or day <= end) for start, end in windows)
