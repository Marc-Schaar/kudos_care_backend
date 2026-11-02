"""
Alle Zustandswechsel von Teilen und Baugruppen an einer Stelle: aufziehen, abnehmen,
ausmustern, wiederherstellen, umziehen, Laufrad wechseln.

Die Views rufen nur diese Funktionen auf. Sie halten die Nutzungszeiträume
(`ComponentUsagePeriod`, `AssemblyUsagePeriod`) richtig, aus denen sich alle
Kilometer ergeben (siehe `api/usage.py`), und rechnen danach den Wetter-Verschleiß
der betroffenen Teile nach.

Datumsangaben sind optional: ohne Angabe gilt "jetzt", ein Tag in der Vergangenheit
gilt ab dem Morgen dieses Tages (`usage.moment_for`). Wer eine Geschichte erzählt, die
nicht aufgeht (Tausch vor dem Einbau), bekommt einen 400 statt stiller Korrektur.
"""

import datetime
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field

from django.db import transaction
from rest_framework.exceptions import ValidationError

from ..models import (
    AssemblyStatus,
    Bike,
    BikeAssembly,
    Component,
    ComponentSlot,
    ComponentStatus,
    ComponentUsagePeriod,
    GroupKind,
    MaintenanceInterval,
    MaintenanceLog,
)
from .services import WeatherWearService
from .usage import (
    intersect_windows,
    moment_for,
    ride_clock_now,
    start_of_day,
)

logger = logging.getLogger("my_app_debug")


def _reject(message: str, code: str) -> None:
    raise ValidationError({"error": message, "code": code})


def _moment(on: datetime.date | None) -> datetime.datetime:
    if on is not None and on > ride_clock_now().date():
        _reject("Das Datum darf nicht in der Zukunft liegen.", "future_date")
    return moment_for(on)


def _refresh_weather(components: Iterable[Component | None]) -> None:
    """
    Rechnet den Wetter-Verschleiß nach. Darf den Wechsel selbst nie scheitern lassen:
    die Zahl ist abgeleitet und wird beim nächsten Ride-Import ohnehin neu berechnet.
    """
    for component in components:
        if component is None:
            continue
        try:
            WeatherWearService.recompute_component(component)
        except Exception:
            logger.exception(
                "Wetter-Verschleiss-Berechnung fuer Komponente %s fehlgeschlagen.",
                component.pk,
            )


# ── Einzelne Teile ───────────────────────────────────────────────────────────────


def _mounted_in(slot: ComponentSlot, exclude: Component | None = None):
    mounted = Component.objects.mounted().filter(slot=slot)
    if exclude is not None:
        mounted = mounted.exclude(pk=exclude.pk)
    return mounted.select_related("slot__assembly", "slot__template").first()


def _reject_if_retired_slot(slot: ComponentSlot) -> None:
    if slot.assembly is not None and slot.assembly.is_retired:
        _reject(
            f"'{slot.assembly.display_name}' ist ausgemustert. Ziehe das Teil zuerst "
            "auf eine andere Baugruppe um.",
            "assembly_retired",
        )


def _take_off(component: Component, at: datetime.datetime, status: str) -> None:
    """
    Beendet den Einsatz eines Teils. Liegt `at` in einer Zeit, in der das Laufrad im
    Keller lag, endet der Einsatz ohne weitere km; reicht `at` dagegen in einen früheren
    Einsatz oder vor den Einbau zurück, passt die Geschichte nicht.
    """
    if component.installed_at is not None and at.date() < component.installed_at:
        _reject(
            f"'{component.label}' ist erst seit "
            f"{component.installed_at:%d.%m.%Y} am Rad. Das Datum muss danach liegen.",
            "date_before_mount",
        )
    if component.periods.filter(ended_at__gt=at).exists():
        _reject(
            f"'{component.label}' war nach diesem Datum noch im Einsatz. Das Datum "
            "muss nach dem letzten Einsatz liegen.",
            "date_before_mount",
        )
    open_period = component.periods.filter(ended_at__isnull=True).first()
    ended_at = max(at, open_period.started_at) if open_period else at

    component.status = status
    component.retired_at = (
        ended_at.date() if status == ComponentStatus.RETIRED else None
    )
    component.save(
        update_fields=["status", "retired_at", "updated_at"], usage_at=ended_at
    )


def _windows_from(slot: ComponentSlot, at: datetime.datetime):
    """
    Die Zeiträume, in denen ein ab `at` montiertes Teil tatsächlich am Rad war. Der
    naheliegende Ablauf „Baugruppe heute anlegen, dann die Teile auf ihr echtes Datum
    setzen" zieht dabei den Beginn der Baugruppe mit zurück; eine Zeit im Keller
    dazwischen fällt heraus.
    """
    assembly = slot.assembly
    if assembly is None:
        return [(at, None)]
    assembly.pull_first_period_back(at)
    return intersect_windows(
        (at, None),
        [(p.started_at, p.ended_at) for p in assembly.periods.order_by("started_at")],
    )


def _put_on(component: Component, at: datetime.datetime) -> None:
    last_end = (
        component.periods.exclude(ended_at__isnull=True)
        .order_by("-ended_at")
        .values_list("ended_at", flat=True)
        .first()
    )
    if last_end is not None and at < last_end:
        _reject(
            f"'{component.label}' war bis {last_end:%d.%m.%Y} noch anderweitig im "
            "Einsatz. Das Datum muss danach liegen.",
            "date_before_mount",
        )

    windows = _windows_from(component.slot, at)
    open_start = next((start for start, end in windows if end is None), at)

    component.status = ComponentStatus.MOUNTED
    component.retired_at = None
    if component.installed_at is None:
        component.installed_at = at.date()
    component.save(
        update_fields=["status", "retired_at", "installed_at", "updated_at"],
        usage_at=open_start,
    )
    for start, end in windows:
        if end is not None:
            ComponentUsagePeriod.objects.create(
                component=component, started_at=start, ended_at=end
            )


def _mount(component: Component, at: datetime.datetime) -> Component | None:
    """Montiert ein Teil und nimmt das bisherige ab. Gibt das abgenommene zurück."""
    _reject_if_retired_slot(component.slot)
    if component.is_mounted:
        return None
    current = _mounted_in(component.slot, exclude=component)
    if current is not None:
        _take_off(current, at, ComponentStatus.SPARE)
    _put_on(component, at)
    _refresh_weather([current, component])
    return current


def mount_component(component: Component, on: datetime.date | None = None) -> None:
    """Reifen A runter, Reifen B drauf: ab jetzt zählen die km für B."""
    at = _moment(on)
    with transaction.atomic():
        _mount(component, at)


def unmount_component(component: Component, on: datetime.date | None = None) -> None:
    """Abnehmen, aber behalten — das Teil liegt ab jetzt als Ersatz im Regal."""
    if not component.is_mounted:
        _reject(f"'{component.label}' ist nicht montiert.", "not_mounted")
    at = _moment(on)
    with transaction.atomic():
        _take_off(component, at, ComponentStatus.SPARE)
        _refresh_weather([component])


def retire_component(component: Component, on: datetime.date | None = None) -> None:
    """Verschlissen oder entsorgt. Bleibt als Historie erhalten."""
    if component.is_retired:
        return
    at = _moment(on)
    with transaction.atomic():
        _take_off(component, at, ComponentStatus.RETIRED)
        # Ein ausgemustertes Teil darf beim nächsten Laufradwechsel nicht zurückkehren.
        component.linked_to_assemblies.clear()
        _refresh_weather([component])


def restore_component(component: Component) -> None:
    """Aus Versehen ausgemustert, oder doch noch gut: zurück ins Regal."""
    if not component.is_retired:
        return
    component.status = ComponentStatus.SPARE
    component.retired_at = None
    component.save(update_fields=["status", "retired_at", "updated_at"])


def move_component(
    component: Component,
    target: ComponentSlot,
    mount: bool = True,
    on: datetime.date | None = None,
) -> None:
    """
    Zieht ein Teil auf einen anderen Slot desselben Typs um — der Reifen vom
    Alu-Laufrad wandert aufs Carbon-Laufrad. Seine km bleiben ihm: sie hängen an den
    Zeiträumen des Teils, nicht am Slot.
    """
    if target.pk == component.slot_id:
        _reject("Das Teil sitzt bereits dort.", "same_slot")
    if (
        target.bike_id != component.slot.bike_id
        or target.template_id != component.slot.template_id
    ):
        _reject(
            "Das Teil passt nicht auf diesen Platz. Umziehen geht nur auf einen Platz "
            "gleicher Art am selben Rad.",
            "slot_mismatch",
        )
    _reject_if_retired_slot(target)

    at = _moment(on)
    with transaction.atomic():
        if component.is_mounted:
            _take_off(component, at, ComponentStatus.SPARE)
        component.slot = target
        component.status = ComponentStatus.SPARE
        component.retired_at = None
        component.save(update_fields=["slot", "status", "retired_at", "updated_at"])
        if mount:
            _mount(component, at)
        else:
            _refresh_weather([component])


def add_component(
    slot: ComponentSlot,
    *,
    mount: bool = True,
    on: datetime.date | None = None,
    **fields,
) -> Component:
    """
    Legt ein neues Teil an. `mount=False` legt es direkt ins Regal (der Winterreifen,
    der erst im November drauf soll): dann gibt es noch keinen Einbau und keine km.
    """
    _reject_if_retired_slot(slot)
    at = _moment(on)
    with transaction.atomic():
        component = Component(slot=slot, status=ComponentStatus.SPARE, **fields)
        if prior := fields.get("prior_km"):
            component.prior_weather_km = prior
        component.save()
        if mount:
            current = _mounted_in(slot)
            if current is not None:
                _take_off(current, at, ComponentStatus.SPARE)
            component.installed_at = on or at.date()
            component.save(update_fields=["installed_at", "updated_at"])
            _put_on(component, at)
            _refresh_weather([current, component])
    return component


def set_installed_on(component: Component, day: datetime.date) -> None:
    """
    Korrigiert den Tag des ersten Einbaus: alle km ab diesem Tag zählen, die davor
    nicht. Gedacht für den Normalfall, dass das Rad erst nach den Fahrten in die App
    kam — das Teil zeigt dann sofort die richtige Zahl.
    """
    if day > ride_clock_now().date():
        _reject(
            "Das Einbaudatum darf nicht in der Zukunft liegen.",
            "future_date",
        )
    start = start_of_day(day)
    with transaction.atomic():
        periods = list(component.periods.order_by("started_at"))
        survivors = [p for p in periods if p.ended_at is None or p.ended_at > start]
        for period in periods:
            if period not in survivors:
                period.delete()

        component.installed_at = day
        component.save(update_fields=["installed_at", "updated_at"])

        if survivors:
            first = survivors[0]
            ended_at = first.ended_at
            first.delete()
            assembly = component.slot.assembly
            if assembly is None:
                windows = [(start, ended_at)]
            else:
                assembly.pull_first_period_back(start)
                windows = intersect_windows(
                    (start, ended_at),
                    [
                        (p.started_at, p.ended_at)
                        for p in assembly.periods.order_by("started_at")
                    ],
                )
            for window_start, window_end in windows:
                ComponentUsagePeriod.objects.create(
                    component=component, started_at=window_start, ended_at=window_end
                )
        _refresh_weather([component])


def set_installed_on_bulk(
    bike: Bike, day: datetime.date, assembly: BikeAssembly | None = None
) -> tuple[int, int]:
    """
    Setzt das Einbaudatum aller montierten Teile, wahlweise für eine Baugruppe. Ohne
    Baugruppe nur für das, was gerade am Rad ist: ein geparkter Satz hat seine eigene
    Geschichte, die ein Stichtag fürs Rad nicht überschreiben darf.
    """
    components = Component.objects.mounted().filter(slot__bike=bike)
    if assembly is not None:
        components = components.filter(slot__assembly=assembly)
    else:
        components = components.filter(
            slot__in=ComponentSlot.objects.on_bike(bike),
        )

    assemblies: dict[int, BikeAssembly] = {}
    updated = 0
    with transaction.atomic():
        for component in components.select_related("slot__assembly", "slot__template"):
            set_installed_on(component, day)
            updated += 1
            if component.slot.assembly is not None:
                assemblies[component.slot.assembly.pk] = component.slot.assembly
        for asm in assemblies.values():
            if asm.installed_at is None or asm.installed_at > day:
                asm.installed_at = day
                asm.save(update_fields=["installed_at", "updated_at"])
    return updated, len(assemblies)


def set_prior_km(component: Component, prior_km: float | None) -> None:
    """Die Wetter-Achse bekommt dieselbe Vorgeschichte: über deren Wetter wissen wir nichts."""
    delta = (prior_km or 0.0) - (component.prior_km or 0.0)
    component.prior_km = prior_km
    component.prior_weather_km = (component.prior_weather_km or 0.0) + delta
    component.save(update_fields=["prior_km", "prior_weather_km", "updated_at"])
    _refresh_weather([component])


# ── Baugruppen ───────────────────────────────────────────────────────────────────


def active_sibling(bike: Bike, assembly_group) -> BikeAssembly | None:
    """Die aktuell aufgezogene Instanz derselben Baugruppe an diesem Bike."""
    return (
        BikeAssembly.objects.filter(bike=bike, group=assembly_group)
        .active()
        .select_related("bike", "group")
        .first()
    )


def _linked_closure(assembly: BikeAssembly) -> list[BikeAssembly]:
    """Die Baugruppe plus alles, was direkt oder über Umwege an ihr hängt."""
    seen: dict[int, BikeAssembly] = {}
    queue = [assembly]
    while queue:
        current = queue.pop(0)
        if current.pk in seen:
            continue
        seen[current.pk] = current
        queue.extend(
            current.linked_assemblies.exclude(
                status=AssemblyStatus.RETIRED
            ).select_related("group", "bike")
        )
    return list(seen.values())


def _with_linked(assembly: BikeAssembly) -> list[BikeAssembly]:
    """Was gemeinsam aufgezogen wird — je Katalog-Gruppe höchstens eine Instanz."""
    members: dict[int, BikeAssembly] = {}
    for member in _linked_closure(assembly):
        members.setdefault(member.group_id, member)
    return list(members.values())


@dataclass
class PlannedMount:
    component: Component
    replaces: Component | None
    via: BikeAssembly


@dataclass
class ActivationPlan:
    """Was ein Wechsel auslöst — für die Vorschau genauso wie für die Ausführung."""

    activate: list[BikeAssembly] = field(default_factory=list)
    park: list[BikeAssembly] = field(default_factory=list)
    mount: list[PlannedMount] = field(default_factory=list)
    #: (Teil, abgezogene Baugruppe): gehört zur abgezogenen Baugruppe, bleibt aber
    #: montiert, weil zur neuen nichts hinterlegt ist. Die Vorschau weist darauf hin.
    unpaired: list[tuple[Component, BikeAssembly]] = field(default_factory=list)
    #: incoming.pk → die dadurch abgezogene Instanz.
    outgoing: dict[int, BikeAssembly] = field(default_factory=dict)


def plan_activation(assembly: BikeAssembly) -> ActivationPlan:
    """Ermittelt, was beim Aufziehen dieser Baugruppe alles mitwechselt."""
    members = _with_linked(assembly)
    member_ids = {member.pk for member in members}
    plan = ActivationPlan()

    for member in members:
        if member.is_active:
            continue
        plan.activate.append(member)
        sibling = active_sibling(member.bike, member.group)
        if sibling is not None and sibling.pk not in member_ids:
            plan.park.append(sibling)
            plan.outgoing[member.pk] = sibling

    planned_slots: set[int] = set()
    for member in members:
        linked = member.linked_components.exclude(
            status=ComponentStatus.RETIRED
        ).select_related("slot__assembly", "slot__template")
        for component in linked:
            if component.slot_id in planned_slots:
                continue
            planned_slots.add(component.slot_id)
            if component.is_mounted:
                continue
            if (
                component.slot.assembly is not None
                and component.slot.assembly.is_retired
            ):
                continue
            plan.mount.append(
                PlannedMount(
                    component=component,
                    replaces=_mounted_in(component.slot),
                    via=member,
                )
            )

    for outgoing in plan.park:
        stays = (
            outgoing.linked_components.mounted()
            .exclude(slot_id__in=planned_slots)
            .select_related("slot__template")
        )
        plan.unpaired.extend((component, outgoing) for component in stays)

    return plan


def _sync_parts(assembly: BikeAssembly, at: datetime.datetime) -> list[Component]:
    """Baugruppe auf- oder abgezogen: die Zeiträume ihrer Teile laufen mit an bzw. aus."""
    parts = list(
        Component.objects.mounted()
        .filter(slot__assembly=assembly)
        .select_related("slot__assembly", "slot__template")
    )
    for part in parts:
        part.sync_usage_period(at)
    return parts


def _check_switch_moment(plan: ActivationPlan, at: datetime.datetime) -> None:
    for outgoing in plan.park:
        current = outgoing.periods.filter(ended_at__isnull=True).first()
        started_too_late = ComponentUsagePeriod.objects.filter(
            component__slot__assembly=outgoing,
            ended_at__isnull=True,
            started_at__gt=at,
        ).exists() or (current is not None and at < current.started_at)
        if started_too_late:
            _reject(
                f"'{outgoing.display_name}' ist erst danach aufgezogen worden. Das "
                "Datum muss danach liegen.",
                "date_before_mount",
            )
    for incoming in plan.activate:
        if incoming.periods.filter(ended_at__gt=at).exists():
            _reject(
                f"'{incoming.display_name}' war nach diesem Datum noch am Rad. Das "
                "Datum muss danach liegen.",
                "date_before_mount",
            )


def activate_assembly(
    assembly: BikeAssembly, on: datetime.date | None = None
) -> ActivationPlan:
    """
    Zieht eine Baugruppe auf: was an ihr hängt, wechselt mit (das andere Laufrad des
    Satzes, die passenden Bremsbeläge), und der bisher aufgezogene Satz wird geparkt.
    Alles zum selben Zeitpunkt, damit jede Fahrt genau einem Satz gehört.

    Wird dabei ein Belag abgenommen, merkt sich das abgezogene Laufrad ihn als seinen
    Partner, falls es noch keinen hat. So reicht es, die Kopplung einmal einzurichten:
    der Rückweg stimmt von selbst.
    """
    if assembly.is_retired:
        _reject(
            f"'{assembly.display_name}' ist ausgemustert und kann nicht aufgezogen "
            "werden. Stelle es zuerst wieder her.",
            "retired",
        )
    if assembly.group.kind == GroupKind.AREA:
        sibling = active_sibling(assembly.bike, assembly.group)
        if sibling is not None and sibling.pk != assembly.pk:
            _reject(
                f"'{assembly.group.name}' ist ein Bereich, keine Baugruppe: dort "
                "werden die Teile einzeln getauscht.",
                "not_an_assembly",
            )

    at = _moment(on)
    with transaction.atomic():
        plan = plan_activation(assembly)
        _check_switch_moment(plan, at)

        touched: list[Component] = []
        for outgoing in plan.park:
            outgoing.status = AssemblyStatus.PARKED
            outgoing.save(update_fields=["status", "updated_at"], usage_at=at)
            touched += _sync_parts(outgoing, at)
        for incoming in plan.activate:
            incoming.status = AssemblyStatus.ACTIVE
            incoming.retired_at = None
            incoming.save(
                update_fields=["status", "retired_at", "updated_at"], usage_at=at
            )
            touched += _sync_parts(incoming, at)

        for planned in plan.mount:
            _mount(planned.component, at)
            outgoing = plan.outgoing.get(planned.via.pk)
            if (
                planned.replaces is not None
                and outgoing is not None
                and not outgoing.linked_components.filter(
                    slot_id=planned.replaces.slot_id
                ).exists()
            ):
                outgoing.linked_components.add(planned.replaces)

        _refresh_weather(touched)
    return plan


def retire_assembly(
    assembly: BikeAssembly,
    on: datetime.date | None = None,
    with_linked: bool = False,
) -> list[BikeAssembly]:
    """
    Mustert eine Baugruppe aus (verkauft/entsorgt): die montierten Teile werden mit
    ausgemustert, Ersatzteile bleiben Ersatz — der gute Reifen im Regal hängt ja nicht
    am kaputten Laufrad. `with_linked` nimmt den ganzen Satz mit. Erneutes Ausmustern
    ändert nichts.
    """
    if assembly.is_retired:
        return [assembly]

    targets = [assembly]
    if with_linked:
        targets = [member for member in _with_linked(assembly) if not member.is_retired]

    at = _moment(on)
    with transaction.atomic():
        for target in targets:
            parts = list(
                Component.objects.mounted()
                .filter(slot__assembly=target)
                .select_related("slot__assembly", "slot__template")
            )
            for part in parts:
                _take_off(part, at, ComponentStatus.RETIRED)
            target.status = AssemblyStatus.RETIRED
            target.retired_at = at.date()
            target.save(
                update_fields=["status", "retired_at", "updated_at"], usage_at=at
            )
            _refresh_weather(parts)
    return targets


def restore_assembly(assembly: BikeAssembly) -> None:
    """
    Holt eine ausgemusterte Baugruppe zurück, samt den Teilen, die an dem Tag mit ihr
    ausgemustert wurden. Ist für die Gruppe schon etwas aufgezogen, kommt sie geparkt
    zurück und verdrängt nichts.
    """
    if not assembly.is_retired:
        return

    bike, group = assembly.bike, assembly.group
    if (
        group.kind == GroupKind.AREA
        and BikeAssembly.objects.filter(bike=bike, group=group)
        .exclude(status=AssemblyStatus.RETIRED)
        .exists()
    ):
        _reject(
            f"Für '{group.name}' gibt es an diesem Rad bereits einen Eintrag.",
            "area_exists",
        )

    retired_day = assembly.retired_at
    with transaction.atomic():
        assembly.status = (
            AssemblyStatus.PARKED
            if active_sibling(bike, group) is not None
            else AssemblyStatus.ACTIVE
        )
        assembly.retired_at = None
        assembly.save(update_fields=["status", "retired_at", "updated_at"])

        for slot in assembly.slots.select_related("assembly"):
            part = (
                slot.components.retired()
                .filter(retired_at=retired_day)
                .order_by("-updated_at")
                .first()
            )
            if part is not None and _mounted_in(slot) is None:
                part.status = ComponentStatus.MOUNTED
                part.retired_at = None
                part.save(update_fields=["status", "retired_at", "updated_at"])


def set_links(
    assembly: BikeAssembly,
    assembly_ids: list[int],
    component_ids: list[int],
) -> None:
    """
    Legt fest, was beim Aufziehen dieser Baugruppe mitwechselt. Gekoppelt werden das
    andere Laufrad des Satzes und einzelne Teile aus Bereichen wie der Bremse — ein
    Reifen hängt ohnehin an seinem Laufrad, und je Slot kann nur ein Teil mitwechseln.
    """
    assemblies = list(
        BikeAssembly.objects.filter(pk__in=assembly_ids, bike=assembly.bike)
        .exclude(pk=assembly.pk)
        .exclude(status=AssemblyStatus.RETIRED)
        .select_related("group")
    )
    if len(assemblies) != len(set(assembly_ids)):
        _reject("Diese Baugruppe lässt sich nicht koppeln.", "link_invalid")

    groups = {assembly.group_id}
    for other in assemblies:
        if other.group_id in groups:
            _reject(
                "Zwei Laufräder derselben Seite können nicht gleichzeitig am Rad sein.",
                "link_invalid",
            )
        groups.add(other.group_id)

    components = list(
        Component.objects.filter(pk__in=component_ids, slot__bike=assembly.bike)
        .exclude(status=ComponentStatus.RETIRED)
        .select_related("slot__assembly__group", "slot__template")
    )
    if len(components) != len(set(component_ids)):
        _reject("Dieses Teil lässt sich nicht koppeln.", "link_invalid")

    slots: set[int] = set()
    for component in components:
        owner = component.slot.assembly
        if owner is not None and (
            owner.pk == assembly.pk or owner.group.kind == GroupKind.ASSEMBLY
        ):
            _reject(
                f"'{component.label}' sitzt auf einem Laufrad und wechselt schon mit "
                "ihm.",
                "link_invalid",
            )
        if component.slot_id in slots:
            _reject(
                f"Für '{component.slot.display_name}' kann nur ein Teil mitwechseln.",
                "link_invalid",
            )
        slots.add(component.slot_id)

    with transaction.atomic():
        assembly.linked_assemblies.set(assemblies)
        assembly.linked_components.set(components)
        # Auch über mehrere Kopplungen hinweg darf nie zweimal dieselbe Seite am Rad landen.
        linked_groups = [member.group_id for member in _linked_closure(assembly)]
        if len(linked_groups) != len(set(linked_groups)):
            _reject(
                "Zwei Laufräder derselben Seite können nicht gleichzeitig am Rad sein.",
                "link_invalid",
            )


# ── Wartungs-Intervalle ──────────────────────────────────────────────────────────


def log_interval_done(
    interval: MaintenanceInterval,
    done_on: datetime.date | None = None,
    note: str = "",
) -> MaintenanceLog:
    """
    "Erledigt": km und Tage zählen ab diesem Moment neu. Ein nachgetragener älterer
    Termin steht nur in der Historie und setzt nichts zurück — sonst liefe ein
    Intervall rückwärts, das längst neuer erledigt wurde.
    """
    if done_on is not None and done_on > ride_clock_now().date():
        _reject("Das Datum darf nicht in der Zukunft liegen.", "future_date")
    day = done_on or ride_clock_now().date()

    with transaction.atomic():
        log = MaintenanceLog.objects.create(
            interval=interval,
            done_at=day,
            note=note,
            previous_done_at=interval.last_done_at,
            previous_baseline_at=interval.km_baseline_at,
        )
        if interval.last_done_at is None or day >= interval.last_done_at:
            interval.last_done_at = day
            interval.km_baseline_at = moment_for(done_on)
            interval.save(
                update_fields=["last_done_at", "km_baseline_at", "updated_at"]
            )
    return log


def undo_interval_log(interval: MaintenanceInterval) -> None:
    """Aus Versehen auf "Erledigt" getippt: stellt den Stand davor exakt wieder her."""
    log = interval.logs.order_by("-id").first()
    if log is None:
        _reject("Es gibt keinen Eintrag zum Zurücknehmen.", "no_log")
    assert log is not None

    with transaction.atomic():
        older = interval.logs.exclude(pk=log.pk).order_by("-id").first()
        interval.last_done_at = log.previous_done_at or (
            older.done_at if older else None
        )
        interval.km_baseline_at = log.previous_baseline_at
        interval.save(update_fields=["last_done_at", "km_baseline_at", "updated_at"])
        log.delete()
