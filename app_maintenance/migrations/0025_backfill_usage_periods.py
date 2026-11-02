"""
Überträgt den Bestand von Tacho-Schnappschüssen auf Nutzungszeiträume.

Ziel: die angezeigten km ändern sich nicht, außer wo sie vorher falsch waren. Das Datum
bleibt dabei maßgeblich, denn das hat der Nutzer genannt; der gespeicherte km-Stand
entscheidet nur, welche Fahrten **desselben Tages** noch davor lagen. War er längst
von der Wirklichkeit überholt (nachimportierte Fahrten), zählt allein das Datum.

Zusätzlich drei Aufräumarbeiten, die erst mit dem neuen Modell möglich sind:

* **Ein Bereich hat nur noch eine Instanz.** Zwei "Bremse hinten" waren der einzige Weg,
  zwei Belag-Sätze abzubilden. Das geht jetzt mit zwei Teilen im selben Platz, die
  geparkte Instanz geht also in der aufgezogenen auf. Ihre Teile liegen danach im Regal.
* **Montierte Teile an ausgemusterten Baugruppen** waren ein Folge-Fehler von `swap`.
  Sie gelten jetzt als ausgemustert.
* **Vorder- und Hinterrad** werden gekoppelt, wo die Zuordnung eindeutig ist (siehe
  `_same_set`).

Nach dieser Migration gehört `manage.py recompute_weather_wear` zum Deploy: der
Wetter-Verschleiß ist gespeichert und zählt jetzt exakt dieselben Fahrten wie die km.
"""

from datetime import datetime, time, timedelta, timezone

from django.db import migrations
from django.utils import timezone as dj_timezone

UTC = timezone.utc
#: Rundungs-Spielraum beim Vergleich von gespeichertem km-Stand und Fahrten-Summe.
KM_EPSILON = 0.05
#: Multiplikator-Obergrenze, siehe `services.MAX_MULTIPLIER`. Die Wetter-Vorgeschichte
#: wurde beim Wiedermontieren doppelt gezählt; mehr als das kann nicht echt sein.
MAX_WEATHER_MULTIPLIER = 2.0


def _midnight(day):
    return datetime.combine(day, time.min, tzinfo=UTC)


def _moment(day, snapshot_km, rides):
    """
    Der Zeitpunkt zu einem Datum samt km-Stand. Fahrten dieses Tages, die im km-Stand
    schon enthalten waren, lagen davor; alles andere danach.
    """
    moment = _midnight(day)
    if snapshot_km is None:
        return moment
    total = 0.0
    for start, km in rides:
        if start.date() > day:
            break
        total += km
        if start.date() < day:
            continue
        if total > snapshot_km + KM_EPSILON:
            break
        moment = start + timedelta(seconds=1)
    return moment


def _merge(windows):
    """Überlappungen zusammenlegen: dieselbe Zeit darf nicht doppelt zählen."""
    merged = []
    for start, end in sorted(windows, key=lambda w: w[0]):
        if merged and (merged[-1][1] is None or start <= merged[-1][1]):
            last_start, last_end = merged[-1]
            if last_end is None or end is None:
                merged[-1] = (last_start, None)
            else:
                merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _intersect(window, others):
    start, end = window
    result = []
    for other_start, other_end in others:
        low = max(start, other_start)
        if end is None or other_end is None:
            high = end or other_end
        else:
            high = min(end, other_end)
        if high is None or high > low:
            result.append((low, high))
    return result


def _backfill_assembly_periods(apps, assembly, rides, now):
    """Rechnet die Perioden einer Baugruppe um und gibt die Fenster zurück."""
    Period = apps.get_model("app_maintenance", "AssemblyUsagePeriod")

    windows = []
    for period in assembly.periods.all():
        start = _moment(period.started_at, period.started_distance_km, rides)
        end = None
        if period.ended_at is not None:
            end = _moment(period.ended_at, period.ended_distance_km, rides)
        windows.append((start, end))

    if windows and assembly.status != "active":
        # Offene Periode an einer abgezogenen Baugruppe: kann es nicht geben.
        windows = [(start, end or now) for start, end in windows]

    if assembly.status == "active" and not any(end is None for _, end in windows):
        started = windows[-1][1] if windows else None
        if started is None:
            day = assembly.installed_at or assembly.created_at.date()
            started = _midnight(day)
        windows.append((started, None))

    windows = [(s, e) for s, e in windows if e is None or e > s]
    windows = _merge(windows)

    assembly.periods.all().delete()
    for start, end in windows:
        Period.objects.create(
            assembly=assembly,
            started_at=start.date(),
            started_moment=start,
            ended_at=end.date() if end else None,
            ended_moment=end,
        )
    return windows


def _backfill_component(apps, component, assembly_windows_by_id, rides, now):
    Stint = apps.get_model("app_maintenance", "ComponentUsagePeriod")
    assembly = component.slot.assembly

    if component.installed_at is None and component.is_mounted:
        # Vorher blieb so ein Teil für immer auf "unbekannt" stehen.
        component.installed_at = component.created_at.date()

    if assembly is not None and assembly.status == "retired":
        status = "retired"
        retired_at = (
            assembly.retired_at if component.is_mounted else component.retired_at
        ) or assembly.retired_at
    elif component.is_mounted:
        status, retired_at = "mounted", None
    else:
        status, retired_at = "spare", None

    if component.installed_at is not None:
        start = _moment(component.installed_at, component.distance_at_install, rides)
        end = None
        if not component.is_mounted:
            day = component.retired_at or component.updated_at.date()
            end = _moment(day, component.distance_at_retire, rides)

        if end is None or end > start:
            windows = [(start, end)]
            if assembly is not None and assembly_windows_by_id.get(assembly.pk):
                windows = _intersect(windows[0], assembly_windows_by_id[assembly.pk])
            elif assembly is not None and assembly.status != "active":
                windows = []
            for window_start, window_end in windows:
                Stint.objects.create(
                    component=component, started_at=window_start, ended_at=window_end
                )

    component.status = status
    component.retired_at = retired_at
    if component.prior_km is not None or component.prior_weather_km is not None:
        cap = (component.prior_km or 0.0) * MAX_WEATHER_MULTIPLIER
        component.prior_weather_km = min(component.prior_weather_km or 0.0, cap)
    component.save(
        update_fields=["status", "retired_at", "installed_at", "prior_weather_km"]
    )


def _merge_area_instances(apps, bike, now):
    """Je Bereich bleibt eine Instanz; die geparkten gehen in ihr auf."""
    Assembly = apps.get_model("app_maintenance", "BikeAssembly")
    Group = apps.get_model("app_maintenance", "ComponentGroup")
    Slot = apps.get_model("app_maintenance", "ComponentSlot")
    Component = apps.get_model("app_maintenance", "Component")
    Interval = apps.get_model("app_maintenance", "MaintenanceInterval")
    Period = apps.get_model("app_maintenance", "AssemblyUsagePeriod")
    Stint = apps.get_model("app_maintenance", "ComponentUsagePeriod")

    for group in Group.objects.filter(kind="area"):
        instances = list(
            Assembly.objects.filter(bike=bike, group=group)
            .exclude(status="retired")
            .order_by("-updated_at")
        )
        if not instances:
            continue
        keep = next((a for a in instances if a.status == "active"), instances[0])

        if keep.status != "active":
            # Ein Bereich ist per Definition am Rad; "abgezogen" gibt es dort nicht mehr.
            keep.status = "active"
            keep.save(update_fields=["status"])
            Period.objects.create(
                assembly=keep,
                started_at=now.date(),
                started_moment=now,
            )
            for component in Component.objects.filter(
                slot__assembly=keep, status="mounted"
            ):
                Stint.objects.create(component=component, started_at=now)

        for other in instances:
            if other.pk == keep.pk:
                continue
            for slot in Slot.objects.filter(assembly=other):
                Component.objects.filter(slot=slot, status="mounted").update(
                    status="spare"
                )
                target = Slot.objects.filter(
                    assembly=keep, template_id=slot.template_id
                ).first()
                if target is None:
                    slot.assembly = keep
                    slot.save(update_fields=["assembly"])
                else:
                    Component.objects.filter(slot=slot).update(slot=target)
                    slot.delete()
            for interval in Interval.objects.filter(assembly=other):
                twin = (
                    Interval.objects.filter(assembly=keep, label=interval.label)
                    .exclude(pk=interval.pk)
                    .first()
                )
                if twin is None:
                    interval.assembly = keep
                    interval.save(update_fields=["assembly"])
                else:
                    interval.logs.update(interval=twin)
                    interval.delete()
            other.delete()


def _same_set(front, rear):
    """
    Gehören zwei Laufräder sichtbar zusammen? Beide ohne eigenen Namen, oder derselbe
    Name vorne ("Carbon vorne"/"Carbon hinten"). Dass zwei Räder gerade zusammen am Rad
    sind, reicht nicht: wer nur vorne gewechselt hat, fährt zwei verschiedene Sätze.
    """
    if not front.name and not rear.name:
        return True
    front_words, rear_words = front.name.lower().split(), rear.name.lower().split()
    return bool(front_words and rear_words and front_words[0] == rear_words[0])


def _link_wheel_pairs(apps, bike):
    Assembly = apps.get_model("app_maintenance", "BikeAssembly")
    for status in ("active", "parked"):
        wheels = list(
            Assembly.objects.filter(
                bike=bike, group__kind="assembly", status=status
            ).select_related("group")
        )
        front = [w for w in wheels if w.group.position == "front"]
        rear = [w for w in wheels if w.group.position == "rear"]
        if len(front) == 1 and len(rear) == 1 and _same_set(front[0], rear[0]):
            # Im historischen Model ist die Beziehung nicht symmetrisch: beide Richtungen.
            front[0].linked_assemblies.add(rear[0])
            rear[0].linked_assemblies.add(front[0])


def backfill(apps, schema_editor):
    Bike = apps.get_model("app_maintenance", "Bike")
    Assembly = apps.get_model("app_maintenance", "BikeAssembly")
    Component = apps.get_model("app_maintenance", "Component")
    Check = apps.get_model("app_maintenance", "ComponentCheck")
    Interval = apps.get_model("app_maintenance", "MaintenanceInterval")
    Template = apps.get_model("app_maintenance", "ComponentTemplate")
    Slot = apps.get_model("app_maintenance", "ComponentSlot")
    Ride = apps.get_model("app_dashboard", "Ride")

    now = dj_timezone.now()

    Bike.objects.filter(strava_bike_id="").update(strava_bike_id=None)

    for template in Template.objects.filter(is_system=False, owner__isnull=True):
        slot = Slot.objects.filter(template=template).select_related("bike").first()
        if slot is not None:
            template.owner_id = slot.bike.athlete_id
            template.save(update_fields=["owner"])

    for prefix in ("Bremsbel", "Bremsscheibe"):
        Template.objects.filter(name__startswith=prefix).update(follows_wheel=True)

    for bike in Bike.objects.all():
        rides = sorted(
            (
                (start, distance / 1000)
                for start, distance in Ride.objects.filter(
                    bike=bike, start_date__isnull=False, distance__isnull=False
                ).values_list("start_date", "distance")
            ),
            key=lambda ride: ride[0],
        )

        assembly_windows = {
            assembly.pk: _backfill_assembly_periods(apps, assembly, rides, now)
            for assembly in Assembly.objects.filter(bike=bike)
        }

        for component in Component.objects.filter(slot__bike=bike).select_related(
            "slot__assembly"
        ):
            _backfill_component(apps, component, assembly_windows, rides, now)

        for check in Check.objects.filter(component__slot__bike=bike):
            check.baseline_at = _moment(
                check.checked_at, check.checked_at_distance_km, rides
            )
            check.save(update_fields=["baseline_at"])

        for interval in Interval.objects.filter(bike=bike, last_done_at__isnull=False):
            interval.km_baseline_at = _moment(
                interval.last_done_at, interval.last_done_distance_km, rides
            )
            interval.save(update_fields=["km_baseline_at"])

        _merge_area_instances(apps, bike, now)
        _link_wheel_pairs(apps, bike)


class Migration(migrations.Migration):

    dependencies = [
        ("app_dashboard", "__latest__"),
        ("app_maintenance", "0024_usage_periods_schema"),
    ]

    operations = [
        # Rückwärts gibt es nichts wiederherzustellen: die Schnappschüsse sind danach weg.
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
