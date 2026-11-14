"""
Wann war ein Teil am Rad, und wie viele km fallen in diese Zeit — die **einzige**
Quelle dafür.

Die Regel ist genau ein Satz: **Eine Fahrt gehört den Teilen, die zu ihrem
Startzeitpunkt am Rad waren.** Kilometer werden nie gespeichert, sondern aus den
Fahrten in den Nutzungszeiträumen (`ComponentUsagePeriod`, `AssemblyUsagePeriod`)
live zusammengezählt.

Vorher hingen zwei Rechnungen nebeneinander: die km-Achse rechnete mit
gespeicherten Tacho-Schnappschüssen, die Wetter-Achse mit Datumsangaben. Sobald
sich die Fahrten änderten, liefen sie auseinander — eine erst nach dem Reifentausch
synchronisierte Fahrt landete beim neuen Reifen, eine gelöschte Fahrt warf die
Schnappschüsse aus der Skala. Jetzt zählen beide Achsen dieselben Fahrten, und ein
Nachimport oder Löschen stimmt automatisch.

**Die Uhr der Fahrten:** `Ride.start_date` kommt aus Stravas `start_date_local`, ist
also die Ortszeit, nur als UTC beschriftet. Alle Nutzungszeiträume liegen deshalb auf
derselben Uhr (`ride_clock_now()`). Mit dem echten `timezone.now()` zählte eine Fahrt
um 10 Uhr, nach der man um 11 Uhr die Reifen wechselt, zum neuen Reifen — in Deutschland
liegt die echte UTC-Zeit dann noch vor der beschrifteten Startzeit.

Fenster sind halboffen: `start <= Fahrt < ende`. Beim Tausch A → B endet A genau dort,
wo B beginnt, jede Fahrt gehört also genau einem Teil.

Die **Tage-Achse bleibt bewusst außen vor**: Gummi und Dichtmilch altern auch im
Keller, `wear_days` zählt über Parkzeiten hinweg weiter (siehe `compute_wear`).
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Iterable
from datetime import date, datetime, time
from datetime import timezone as dt_timezone
from itertools import accumulate
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from django.utils import timezone

if TYPE_CHECKING:
    from ..models import BikeAssembly, Component, MaintenanceInterval

#: Zeitzone der Nutzer für die "Jetzt"-Angabe. Strava liefert die Ortszeit der Fahrt,
#: den Server interessiert hier nur, wie sich "jetzt" dazu verhält.
RIDE_CLOCK_ZONE = ZoneInfo("Europe/Berlin")

#: `None` als Start = seit jeher, `None` als Ende = bis heute offen.
Window = tuple[datetime | None, datetime | None]


def ride_clock_now() -> datetime:
    """Jetzt, auf der Uhr von `Ride.start_date` (siehe Modul-Docstring)."""
    return timezone.now().astimezone(RIDE_CLOCK_ZONE).replace(tzinfo=dt_timezone.utc)


def start_of_day(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=dt_timezone.utc)


def moment_for(day: date | None) -> datetime:
    """
    Der Zeitpunkt zu einer Tagesangabe des Nutzers. Heute (oder keine Angabe) heißt
    "jetzt": was heute schon gefahren wurde, gehört noch zum alten Teil. Ein Tag in
    der Vergangenheit heißt "ab morgens": die Fahrten dieses Tages zählen schon für
    das neue Teil, denn wer nachträglich einen Tag nennt, meint den Tag des Umbaus.
    """
    now = ride_clock_now()
    if day is None or day >= now.date():
        return now
    return start_of_day(day)


class RideLedger:
    """
    Die Fahrten eines Bikes als kumulierte km — beantwortet "wie viele km liegen in
    diesem Zeitraum" in O(log n). Einmal je Request und Bike aufgebaut (siehe
    `api/serializers.py::ride_ledger`), damit die Anzahl der Teile keine Queries
    kostet.
    """

    def __init__(self, rides: Iterable[tuple[datetime, float]] = ()):
        ordered = sorted(rides, key=lambda ride: ride[0])
        self._starts = [start for start, _ in ordered]
        self._cumulative = list(accumulate((km for _, km in ordered), initial=0.0))

    @classmethod
    def for_bike(cls, bike_id: int) -> "RideLedger":
        from app_dashboard.models import Ride

        rows = Ride.objects.filter(
            bike_id=bike_id, start_date__isnull=False, distance__isnull=False
        ).values_list("start_date", "distance")
        return cls((start, distance / 1000) for start, distance in rows)

    def km_between(self, start: datetime | None, end: datetime | None) -> float:
        low = 0 if start is None else bisect_left(self._starts, start)
        high = len(self._starts) if end is None else bisect_left(self._starts, end)
        return self._cumulative[high] - self._cumulative[low] if high > low else 0.0

    def km_in(self, windows: Iterable[Window]) -> float:
        return sum(self.km_between(start, end) for start, end in windows)


def clip_windows(windows: Iterable[Window], since: datetime | None) -> list[Window]:
    """Schneidet Fenster unten ab — für die Baseline nach einer Prüfung/Erledigung."""
    if since is None:
        return list(windows)
    clipped: list[Window] = []
    for start, end in windows:
        if end is not None and end <= since:
            continue
        clipped.append((since if start is None or start < since else start, end))
    return clipped


def intersect_windows(window: Window, others: Iterable[Window]) -> list[Window]:
    """
    Der Teil von `window`, der in `others` liegt. Gebraucht beim Rückdatieren: ein
    Einbaudatum darf nicht in die Zeit reichen, in der die Baugruppe im Keller lag.
    """
    start, end = window
    result: list[Window] = []
    for other_start, other_end in others:
        low = max(start, other_start) if start and other_start else start or other_start
        if end is None or other_end is None:
            high = end or other_end
        else:
            high = min(end, other_end)
        if high is None or low is None or high > low:
            result.append((low, high))
    return result


def component_windows(component: "Component") -> list[Window]:
    """In welchen Zeiträumen die Komponente tatsächlich gefahren wurde."""
    return [(p.started_at, p.ended_at) for p in component.periods.all()]


def assembly_windows(assembly: "BikeAssembly") -> list[Window]:
    """In welchen Zeiträumen die Baugruppe am Rad war."""
    return [(p.started_at, p.ended_at) for p in assembly.periods.all()]


def component_km(
    component: "Component", ledger: RideLedger, since: datetime | None = None
) -> float:
    """
    Auf dieser Komponente gefahrene km, ohne `prior_km`. `since` ist die Baseline nach
    einer Freigabe (`ComponentCheck.baseline_moment`).
    """
    return round(ledger.km_in(clip_windows(component_windows(component), since)), 1)


def assembly_km(assembly: "BikeAssembly", ledger: RideLedger) -> float:
    return round(ledger.km_in(assembly_windows(assembly)), 1)


def interval_km_since_done(
    interval: "MaintenanceInterval", ledger: RideLedger
) -> float:
    """
    Km seit der letzten Erledigung. Hängt das Intervall an einer Baugruppe, zählen nur
    deren Zeiten am Rad — Dichtmilch im Laufrad im Keller sammelt keine km.
    """
    windows: list[Window] = (
        assembly_windows(interval.assembly)
        if interval.assembly_id is not None
        else [(None, None)]
    )
    return round(ledger.km_in(clip_windows(windows, interval.km_baseline())), 1)


def date_in_windows(windows: Iterable[Window], moment: datetime) -> bool:
    """War das Teil zu diesem Zeitpunkt am Rad?"""
    return any(
        (start is None or start <= moment) and (end is None or moment < end)
        for start, end in windows
    )
