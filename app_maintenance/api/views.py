import datetime
import logging

from django.db import transaction
from django.db.models import Prefetch, ProtectedError
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from app_auth.consent import ai_consent_required_response, has_ai_consent
from app_auth.models import StravaProfile
from app_maintenance.models import (
    AssemblyStatus,
    Bike,
    BikeAssembly,
    Component,
    ComponentCheck,
    ComponentGroup,
    ComponentSlot,
    ComponentStatus,
    ComponentTemplate,
    GroupKind,
    MaintenanceInterval,
    MaintenanceIntervalKind,
    MaintenanceKind,
)

from . import lifecycle
from .ai_providers import generate_reviewed_text
from .bike_assistant import suggest_models, suggest_setup
from .serializers import (
    AssemblyActionSerializer,
    AssemblyAddItemRequestSerializer,
    AssemblyCreateRequestSerializer,
    AssemblyLinksSerializer,
    AssistantModelsRequestSerializer,
    AssistantSetupRequestSerializer,
    BikeAssemblySerializer,
    BikeInstalledAtRequestSerializer,
    BikeListSerializer,
    BikeSerializer,
    ComponentActionSerializer,
    ComponentCheckCreateSerializer,
    ComponentCreateSerializer,
    ComponentGroupSerializer,
    ComponentMoveSerializer,
    ComponentSerializer,
    ComponentSlotSerializer,
    ComponentTemplateSerializer,
    MaintenanceIntervalCreateSerializer,
    MaintenanceIntervalLogRequestSerializer,
    MaintenanceIntervalSerializer,
    SpareComponentSerializer,
    compute_wear,
)
from .services import (
    WeatherWearService,
    bike_condition_report_is_stale,
    build_bike_condition_report_prompt,
    build_check_instructions_prompt,
    build_weather_explanation_prompt,
)
from .usage import RideLedger, moment_for, ride_clock_now

logger = logging.getLogger("my_app_debug")


class AthleteMixin:
    """
    Stellt get_athlete() bereit und schränkt QuerySets automatisch
    auf den eingeloggten User ein.
    """

    permission_classes = [IsAuthenticated]

    def get_athlete(self) -> StravaProfile:
        athlete_id = self.request.session.get("strava_athlete_id")
        logging.debug(f"Getting athlete for athlete_id: {athlete_id}")
        return get_object_or_404(StravaProfile, strava_athlete_id=athlete_id)


#: Was die Bike-Serializer lesen. `rides` gehört bewusst NICHT dazu: die
#: Gesamtdistanz kommt über `with_total_distance()` als Annotation, und die km der
#: Teile aus dem `RideLedger` — ein Prefetch würde jede Fahrt samt LineString-Geometrie
#: und weather_data-JSON laden, ohne dass ein Serializer sie anfasst.
BIKE_PREFETCH = (
    "slots__template__group",
    "slots__assembly",
    "slots__components__checks",
    "slots__components__periods",
    "intervals__assembly__periods",
)


class BikeListView(AthleteMixin, generics.ListCreateAPIView):
    """
    GET  /api/maintenance/bikes/   → alle Bikes des Users
    POST /api/maintenance/bikes/   → neues Bike anlegen
    """

    def get_queryset(self):
        return (
            Bike.objects.filter(athlete=self.get_athlete())
            .with_total_distance()
            .prefetch_related(*BIKE_PREFETCH)
        )

    def get_serializer_class(self):
        return BikeSerializer if self.request.method == "POST" else BikeListSerializer

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        serializer = self.get_serializer(queryset, many=True)
        return Response({"bikes": serializer.data})

    def perform_create(self, serializer):
        serializer.save(athlete=self.get_athlete())


class BikeDetailView(AthleteMixin, generics.RetrieveUpdateDestroyAPIView):
    """
    GET    /api/maintenance/bikes/{id}/
    PATCH  /api/maintenance/bikes/{id}/
    DELETE /api/maintenance/bikes/{id}/
    """

    serializer_class = BikeSerializer
    http_method_names = ["get", "patch", "delete", "head", "options"]

    def get_queryset(self):
        return (
            Bike.objects.filter(athlete=self.get_athlete())
            .with_total_distance()
            .prefetch_related(*BIKE_PREFETCH, "intervals__logs")
        )


class ComponentTemplateListView(AthleteMixin, generics.ListCreateAPIView):
    """
    GET  /api/maintenance/templates/?bike_type=mtb&category=brakes
    POST /api/maintenance/templates/   → eigene Vorlage anlegen

    Eigene Vorlagen gehören ihrem Anleger: vorher standen sie ohne Besitzer in einem
    gemeinsamen Topf und waren für alle sichtbar und änderbar.
    """

    serializer_class = ComponentTemplateSerializer

    def get_queryset(self):
        qs = ComponentTemplate.objects.visible_to(self.get_athlete())
        category = self.request.query_params.get("category")
        if category:
            qs = qs.filter(category=category)
        return qs

    def filter_queryset(self, queryset):
        """bike_type-Filter braucht Python-Logik (JSONField), daher hier."""
        qs = super().filter_queryset(queryset)
        bike_type = self.request.query_params.get("bike_type")
        if bike_type:
            qs = [t for t in qs if t.applies_to(bike_type)]
        return qs

    def perform_create(self, serializer):
        serializer.save(is_system=False, owner=self.get_athlete())


class ComponentTemplateDetailView(AthleteMixin, generics.RetrieveUpdateDestroyAPIView):
    """
    GET    /api/maintenance/templates/{id}/
    PATCH  /api/maintenance/templates/{id}/   → nur eigene
    DELETE /api/maintenance/templates/{id}/   → nur eigene, und nur wenn unbenutzt
    """

    serializer_class = ComponentTemplateSerializer
    http_method_names = ["get", "patch", "delete", "head", "options"]

    def get_queryset(self):
        athlete = self.get_athlete()
        if self.request.method == "GET":
            return ComponentTemplate.objects.visible_to(athlete)
        return ComponentTemplate.objects.filter(owner=athlete)

    def destroy(self, request, *args, **kwargs):
        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError:
            return Response(
                {
                    "error": "Diese Vorlage wird noch von einem Teil benutzt.",
                    "code": "template_in_use",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )


# ── Plätze (Slots) ───────────────────────────────────────────────────────────────


class ComponentSlotListView(AthleteMixin, generics.ListAPIView):
    """
    GET /api/maintenance/bikes/{bike_id}/slots/?category=brakes&warn=true

    Angelegt wird ein Platz über `assemblies/{id}/items/`, nicht hier.
    """

    serializer_class = ComponentSlotSerializer

    def get_bike(self):
        return get_object_or_404(
            Bike, pk=self.kwargs["bike_id"], athlete=self.get_athlete()
        )

    def get_queryset(self):
        qs = (
            ComponentSlot.objects.filter(bike=self.get_bike())
            .select_related("template__group", "bike")
            .prefetch_related("components__checks", "components__periods")
        )
        category = self.request.query_params.get("category")
        if category:
            qs = qs.filter(template__category=category)
        return qs

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        if request.query_params.get("warn") == "true":
            response.data = [
                s for s in response.data if s["warn_status"] in ("warn", "critical")
            ]
        return response


class ComponentSlotDetailView(AthleteMixin, generics.RetrieveUpdateDestroyAPIView):
    """
    GET    /api/maintenance/slots/{id}/
    PATCH  /api/maintenance/slots/{id}/   → custom_name
    DELETE /api/maintenance/slots/{id}/?confirm=true → Platz samt aller seiner Teile
    """

    serializer_class = ComponentSlotSerializer
    http_method_names = ["get", "patch", "delete", "head", "options"]

    def get_queryset(self):
        return (
            ComponentSlot.objects.filter(bike__athlete=self.get_athlete())
            .select_related("bike", "template__group")
            .prefetch_related("components__checks", "components__periods")
        )

    def destroy(self, request, *args, **kwargs):
        slot = self.get_object()
        parts = slot.components.count()
        if parts and request.query_params.get("confirm") != "true":
            return _confirm_required(
                f"Damit verschwinden {parts} Teil(e) samt ihres Verlaufs.", parts
            )
        slot.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


def _confirm_required(message: str, parts: int) -> Response:
    """Endgültiges Löschen braucht `?confirm=true`, wie beim Konto (app_auth)."""
    return Response(
        {"error": message, "code": "confirm_required", "parts": parts},
        status=status.HTTP_400_BAD_REQUEST,
    )


# ── Components ───────────────────────────────────────────────────────────────────


def _component_queryset(athlete: StravaProfile):
    return (
        Component.objects.filter(slot__bike__athlete=athlete)
        .select_related("slot__bike", "slot__template__group", "slot__assembly__group")
        .prefetch_related("periods", "checks")
    )


class ComponentListView(AthleteMixin, generics.ListCreateAPIView):
    """
    GET  /api/maintenance/slots/{slot_id}/components/
    POST /api/maintenance/slots/{slot_id}/components/

    POST legt ein neues Teil an. Mit `mount` (Default) wird es gleich aufgezogen und
    das bisherige Teil wandert ins Regal, ohne bleibt es selbst im Regal.
    """

    serializer_class = ComponentSerializer

    def get_slot(self):
        return get_object_or_404(
            ComponentSlot.objects.select_related("assembly", "template", "bike"),
            pk=self.kwargs["slot_id"],
            bike__athlete=self.get_athlete(),
        )

    def get_queryset(self):
        return _component_queryset(self.get_athlete()).filter(
            slot_id=self.kwargs["slot_id"]
        )

    def create(self, request, *args, **kwargs):
        slot = self.get_slot()
        body = ComponentCreateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = dict(body.validated_data)
        mount = data.pop("mount")
        on = data.pop("installed_at", None)

        component = lifecycle.add_component(slot, mount=mount, on=on, **data)
        return Response(
            ComponentSerializer(
                _component_queryset(self.get_athlete()).get(pk=component.pk)
            ).data,
            status=status.HTTP_201_CREATED,
        )


class ComponentDetailView(AthleteMixin, generics.RetrieveUpdateDestroyAPIView):
    """
    GET    /api/maintenance/components/{id}/
    PATCH  /api/maintenance/components/{id}/
    DELETE /api/maintenance/components/{id}/

    Der Zustand ändert sich über mount/unmount/retire/restore. Ein korrigiertes
    `installed_at` verschiebt auch, ab wann die km zählen.
    """

    serializer_class = ComponentSerializer
    http_method_names = ["get", "patch", "delete", "head", "options"]

    def get_queryset(self):
        return _component_queryset(self.get_athlete())

    def perform_update(self, serializer):
        data = serializer.validated_data
        new_installed_at = data.pop("installed_at", None)
        prior_changed = "prior_km" in data
        prior_km = data.pop("prior_km", None)

        with transaction.atomic():
            component = serializer.save()
            if new_installed_at is not None:
                lifecycle.set_installed_on(component, new_installed_at)
            if prior_changed:
                lifecycle.set_prior_km(component, prior_km)


class ComponentActionView(AthleteMixin, APIView):
    """Gemeinsamer Rahmen der Aktionen auf einem Teil — gelesen wird danach frisch."""

    def get_component(self, pk: int) -> Component:
        return get_object_or_404(_component_queryset(self.get_athlete()), pk=pk)

    def respond(self, pk: int) -> Response:
        return Response(ComponentSerializer(self.get_component(pk)).data)

    def on_date(self, request) -> datetime.date | None:
        body = ComponentActionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        return body.validated_data.get("on")


class ComponentMountView(ComponentActionView):
    """
    POST /api/maintenance/components/{pk}/mount/   Body: { "on"?: "YYYY-MM-DD" }

    Zieht dieses Teil auf und nimmt das bisherige ab. Ab hier zählen die km für dieses
    Teil, das abgenommene bleibt bei seiner Zahl stehen.
    """

    def post(self, request, pk):
        lifecycle.mount_component(self.get_component(pk), on=self.on_date(request))
        return self.respond(pk)


class ComponentUnmountView(ComponentActionView):
    """POST /api/maintenance/components/{pk}/unmount/ — abnehmen, liegt danach im Regal."""

    def post(self, request, pk):
        lifecycle.unmount_component(self.get_component(pk), on=self.on_date(request))
        return self.respond(pk)


class ComponentRetireView(ComponentActionView):
    """POST /api/maintenance/components/{pk}/retire/ — verschlissen oder entsorgt."""

    def post(self, request, pk):
        lifecycle.retire_component(self.get_component(pk), on=self.on_date(request))
        return self.respond(pk)


class ComponentRestoreView(ComponentActionView):
    """POST /api/maintenance/components/{pk}/restore/ — zurück ins Regal."""

    def post(self, request, pk):
        lifecycle.restore_component(self.get_component(pk))
        return self.respond(pk)


class ComponentMoveView(ComponentActionView):
    """
    POST /api/maintenance/components/{pk}/move/
    Body: { "slot_id": 12, "mount"?: true, "on"?: "YYYY-MM-DD" }

    Zieht ein Teil auf einen anderen Platz gleicher Art um, z.B. einen Reifen auf ein
    anderes Laufrad.
    """

    def post(self, request, pk):
        component = self.get_component(pk)
        body = ComponentMoveSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        target = get_object_or_404(
            ComponentSlot.objects.select_related("assembly"),
            pk=data["slot_id"],
            bike=component.slot.bike,
        )
        lifecycle.move_component(
            component, target, mount=data["mount"], on=data.get("on")
        )
        return self.respond(pk)


class ComponentCheckView(ComponentActionView):
    """
    POST /api/maintenance/components/{pk}/check/
    Body: { "condition_pct"?: 0-100, "snooze_km"?: float, "snooze_days"?: int, "note"?: str }
    DELETE → nimmt die letzte Prüfung zurück.

    Protokolliert eine Prüfung/Freigabe der Komponente ("Freigeben"). Der
    Warn-Status wird danach ab diesem Zeitpunkt neu berechnet — mit dem
    angegebenen Snooze-Intervall, falls keins angegeben wurde mit der
    normalen empfohlenen/individuellen Lebensdauer.
    """

    def post(self, request, pk):
        component = self.get_component(pk)
        serializer = ComponentCheckCreateSerializer(
            data=request.data, context={"component": component}
        )
        serializer.is_valid(raise_exception=True)

        # Auf den neuesten Stand bringen: von hier an zählt die Wetter-Achse neu, die
        # Baseline darf also keine Fahrten seit dem letzten Import verschlucken.
        WeatherWearService.recompute_component(component)

        with transaction.atomic():
            ComponentCheck.objects.create(
                component=component,
                checked_at=ride_clock_now().date(),
                baseline_at=ride_clock_now(),
                checked_at_weather_wear_km=component.weather_wear_km,
                **serializer.validated_data,
            )
            # Wird das Teil später wieder kritisch, soll wieder eine Mail kommen. Der
            # Dedupe vergleicht nur mit dem zuletzt gemeldeten Status, und der stünde
            # sonst noch auf dem Wert von vor der Freigabe.
            component.last_warn_notified_status = ""
            component.save(update_fields=["last_warn_notified_status", "updated_at"])

        return self.respond(pk)

    def delete(self, request, pk):
        component = self.get_component(pk)
        check = component.checks.order_by("-id").first()
        if check is None:
            return Response(
                {
                    "error": "Es gibt keine Prüfung zum Zurücknehmen.",
                    "code": "no_check",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        check.delete()
        return self.respond(pk)


class ComponentWeatherExplanationView(AthleteMixin, APIView):
    """
    GET /api/maintenance/components/{pk}/weather-explanation/?refresh=true

    Liefert eine gecachte, KI-generierte Erklärung der wetterbedingten
    Verschleiß-Zahlen. Regeneriert nur wenn noch keine existiert, die Zahlen
    sich seit der letzten Generierung geändert haben, oder ?refresh=true
    übergeben wurde. Die KI berechnet dabei nichts selbst — sie erklärt nur
    bereits von WeatherWearService berechnete Zahlen in Worten.
    """

    def get(self, request, pk):
        component = get_object_or_404(
            Component, pk=pk, slot__bike__athlete=self.get_athlete()
        )

        if not component.weather_wear_ride_count:
            return Response(
                {
                    "error": "Noch keine wetterbereinigten Verschleißdaten vorhanden.",
                    "code": "no_data",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        force_refresh = request.query_params.get("refresh") == "true"
        is_stale = (
            component.weather_wear_explanation_generated_at is None
            or component.weather_wear_computed_at is None
            or component.weather_wear_explanation_generated_at
            < component.weather_wear_computed_at
        )

        if component.weather_wear_explanation and not is_stale and not force_refresh:
            return Response(
                {
                    "explanation": component.weather_wear_explanation,
                    "generated_at": component.weather_wear_explanation_generated_at,
                    "cached": True,
                }
            )

        wear = compute_wear(component)
        if not has_ai_consent(component.slot.bike.athlete):
            return ai_consent_required_response()

        system_prompt, user_prompt = build_weather_explanation_prompt(component, wear)
        explanation = generate_reviewed_text(system_prompt, user_prompt)

        if explanation is None:
            return Response(
                {
                    "error": "KI-Erklärung aktuell nicht verfügbar. Bitte später erneut versuchen.",
                    "code": "ai_unavailable",
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        component.weather_wear_explanation = explanation
        component.weather_wear_explanation_generated_at = timezone.now()
        component.save(
            update_fields=[
                "weather_wear_explanation",
                "weather_wear_explanation_generated_at",
            ]
        )

        return Response(
            {
                "explanation": explanation,
                "generated_at": component.weather_wear_explanation_generated_at,
                "cached": False,
            }
        )


class ComponentCheckInstructionsView(AthleteMixin, APIView):
    """
    GET /api/maintenance/components/{pk}/check-instructions/?refresh=true

    Liefert eine gecachte, KI-generierte Schritt-für-Schritt-Anleitung, wie der
    Nutzer diese Komponente selbst prüfen kann. Regeneriert nur wenn noch keine
    existiert, sich der Gesamt-Warn-Status seit der letzten Generierung geändert
    hat, oder ?refresh=true übergeben wurde. Die KI erfindet dabei keine eigenen
    Verschleiß-Zahlen — sie bekommt den bereits berechneten Status nur als
    Kontext für die Dringlichkeit.
    """

    def get(self, request, pk):
        component = get_object_or_404(
            Component, pk=pk, slot__bike__athlete=self.get_athlete()
        )

        wear = compute_wear(component)

        force_refresh = request.query_params.get("refresh") == "true"
        is_stale = component.check_instructions_status != wear["warn_status_overall"]

        if component.check_instructions and not is_stale and not force_refresh:
            return Response(
                {
                    "instructions": component.check_instructions,
                    "generated_at": component.check_instructions_generated_at,
                    "cached": True,
                }
            )

        if not has_ai_consent(component.slot.bike.athlete):
            return ai_consent_required_response()

        system_prompt, user_prompt = build_check_instructions_prompt(component, wear)
        instructions = generate_reviewed_text(system_prompt, user_prompt)

        if instructions is None:
            return Response(
                {
                    "error": "KI-Prüfanleitung aktuell nicht verfügbar. Bitte später erneut versuchen.",
                    "code": "ai_unavailable",
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        component.check_instructions = instructions
        component.check_instructions_status = wear["warn_status_overall"]
        component.check_instructions_generated_at = timezone.now()
        component.save(
            update_fields=[
                "check_instructions",
                "check_instructions_status",
                "check_instructions_generated_at",
            ]
        )

        return Response(
            {
                "instructions": instructions,
                "generated_at": component.check_instructions_generated_at,
                "cached": False,
            }
        )


class BikeConditionReportView(AthleteMixin, APIView):
    """
    GET /api/maintenance/bikes/{pk}/condition-report/?refresh=true

    Liefert einen gecachten, KI-generierten Zustandsbericht über alle aktuell
    montierten Komponenten des Bikes. Regeneriert nur wenn noch keiner existiert,
    sich die zugrunde liegenden Zahlen seither geändert haben könnten (siehe
    bike_condition_report_is_stale), oder ?refresh=true übergeben wurde. Die KI
    berechnet dabei nichts selbst — sie fasst nur bereits von compute_wear()
    berechnete Werte in Worten zusammen.
    """

    def get(self, request, pk):
        bike = get_object_or_404(Bike, pk=pk, athlete=self.get_athlete())

        slots = (
            ComponentSlot.objects.on_bike(bike)
            .select_related("template")
            .prefetch_related("components__periods", "components__checks")
        )
        ledger = RideLedger.for_bike(bike.pk)
        component_summaries = []
        for slot in slots:
            comp = slot.mounted_component
            if comp is None:
                continue
            wear = compute_wear(comp, ledger)
            component_summaries.append(
                {
                    "name": slot.display_name,
                    "category": slot.template.get_category_display(),
                    "wear_km": wear["wear_km"],
                    "wear_days": wear["wear_days"],
                    "weather_wear_km": comp.weather_wear_km,
                    "warn_status_overall": wear["warn_status_overall"],
                }
            )

        if not component_summaries:
            return Response(
                {
                    "error": "Keine montierten Komponenten für einen Zustandsbericht vorhanden.",
                    "code": "no_data",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        force_refresh = request.query_params.get("refresh") == "true"
        if (
            bike.condition_report
            and not force_refresh
            and not bike_condition_report_is_stale(bike)
        ):
            return Response(
                {
                    "report": bike.condition_report,
                    "generated_at": bike.condition_report_generated_at,
                    "cached": True,
                }
            )

        if not has_ai_consent(bike.athlete):
            return ai_consent_required_response()

        system_prompt, user_prompt = build_bike_condition_report_prompt(
            bike, component_summaries
        )
        report = generate_reviewed_text(system_prompt, user_prompt)

        if report is None:
            return Response(
                {
                    "error": "KI-Zustandsbericht aktuell nicht verfügbar. Bitte später erneut versuchen.",
                    "code": "ai_unavailable",
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        bike.condition_report = report
        bike.condition_report_generated_at = timezone.now()
        bike.save(update_fields=["condition_report", "condition_report_generated_at"])

        return Response(
            {
                "report": report,
                "generated_at": bike.condition_report_generated_at,
                "cached": False,
            }
        )


# ── Baugruppen (BikeAssembly) & Wartungs-Intervalle ───────────────────────────


def _interval_kind_for_template(template: ComponentTemplate) -> str:
    name = template.name.lower()
    if "dichtmilch" in name:
        return MaintenanceIntervalKind.SEALANT
    if "kette" in name and ("wachs" in name or "öl" in name or "schmier" in name):
        return MaintenanceIntervalKind.CHAIN_LUBE
    if "bremsflüssigkeit" in name:
        return MaintenanceIntervalKind.BRAKE_BLEED
    if "di2" in name or "axs" in name:
        return MaintenanceIntervalKind.DI2_CHARGE
    if "akku" in name or "batterie" in name:
        return MaintenanceIntervalKind.BATTERY
    return MaintenanceIntervalKind.CUSTOM


def _spare_components_for_bike(bike: Bike) -> list[Component]:
    """
    Alle Teile im Regal — Grundlage für den "vorhandenes Teil übernehmen"-Vorschlag.
    Ein Teil bleibt beim Abnehmen an seinem Platz hängen, auch wenn dessen Baugruppe
    längst ausgemustert ist; dann braucht es einen neuen Platz (`orphaned`).

    Bewusst **keine** Reduktion auf einen Kandidaten je Template: keine
    Datums-Heuristik errät zuverlässig "den einen richtigen" Reifen. Der Client zeigt
    bei mehreren Treffern eine Auswahl, sortiert ist nur als sinnvoller Default.
    """
    return list(
        Component.objects.spare()
        .filter(slot__bike=bike)
        .select_related("slot__template__group", "slot__assembly__group", "slot__bike")
        .prefetch_related("periods")
        .order_by("-updated_at", "-id")
    )


def _build_assembly_from_request(
    bike: Bike,
    group: ComponentGroup,
    data: dict,
    activate: bool = True,
) -> BikeAssembly:
    """
    Legt eine BikeAssembly + ihre Slots/Components + MaintenanceIntervals atomar an.
    Erwartet bereits validierte Daten (AssemblyCreateRequestSerializer) und
    vorab-geprüfte Template-Zugehörigkeit zur Gruppe.

    `activate=False` legt die Instanz geparkt an (zweiter Laufradsatz, der erst
    später aufgezogen wird) — dann sammeln ihre Teile auch noch keine km.

    Ein Part-Item mit `existing_slot_id` übernimmt einen bereits vorhandenen,
    ungruppierten Platz samt montiertem Teil und Verlauf; `reuse_component_id` holt
    ein Teil aus dem Regal auf einen neuen Platz dieser Baugruppe. In beiden Fällen
    bleibt die Geschichte des Teils stehen, denn sie hängt an seinen Zeiträumen.
    """
    installed_at = data.get("installed_at") or ride_clock_now().date()
    at = moment_for(installed_at)

    with transaction.atomic():
        assembly = BikeAssembly(
            bike=bike,
            group=group,
            name=data.get("name", "") or "",
            installed_at=installed_at,
            status=(AssemblyStatus.ACTIVE if activate else AssemblyStatus.PARKED),
        )
        assembly.save(usage_at=at)

        templates = {
            t.id: t
            for t in group.templates.visible_to(bike.athlete).filter(
                maintenance_kind__in=[
                    MaintenanceKind.PART,
                    MaintenanceKind.CONSUMABLE,
                ]
            )
        }

        for item in data.get("parts", []):
            if not item["include"]:
                continue
            template = templates[item["template_id"]]

            existing_slot_id = item.get("existing_slot_id")
            if existing_slot_id:
                slot = ComponentSlot.objects.get(
                    pk=existing_slot_id,
                    bike=bike,
                    assembly__isnull=True,
                    template=template,
                )
                slot.assembly = assembly
                slot.save(update_fields=["assembly"])
                mounted = slot.mounted_component
                if mounted is not None:
                    first = mounted.periods.order_by("started_at").first()
                    if first is not None:
                        assembly.pull_first_period_back(first.started_at)
                    mounted.slot = slot
                    mounted.sync_usage_period(at)
                continue

            reuse_component_id = item.get("reuse_component_id")
            if reuse_component_id:
                component = Component.objects.select_related(
                    "slot__template", "slot__assembly"
                ).get(pk=reuse_component_id, slot__bike=bike, slot__template=template)
                slot = ComponentSlot.objects.create(
                    bike=bike, assembly=assembly, template=template
                )
                lifecycle.move_component(component, slot, on=installed_at)
                continue

            slot, _ = ComponentSlot.objects.get_or_create(
                assembly=assembly, template=template, defaults={"bike": bike}
            )
            lifecycle.add_component(
                slot,
                on=installed_at,
                brand=item.get("brand", ""),
                model_name=item.get("model_name", ""),
                custom_warn_km=item.get("custom_warn_km"),
                custom_warn_days=item.get("custom_warn_days"),
            )

        for item in data.get("intervals", []):
            if not item["include"]:
                continue
            template = templates[item["template_id"]]
            MaintenanceInterval.objects.create(
                bike=bike,
                assembly=assembly,
                template=template,
                kind=_interval_kind_for_template(template),
                label=template.name,
                interval_km=item.get("interval_km") or template.warn_km,
                interval_days=item.get("interval_days") or template.warn_days,
                last_done_at=installed_at,
                km_baseline_at=at,
            )

    return assembly


def _validate_assembly_items(
    bike: Bike, group: ComponentGroup, data: dict
) -> str | None:
    """
    Prüft, dass alle referenzierten Templates zur Gruppe + richtigen Art gehören,
    und dass jede referenzierte `existing_slot_id` (vorhandene, montierte
    Komponente übernehmen) bzw. `reuse_component_id` (Teil aus dem Regal holen)
    wirklich zu diesem Bike und Template passt.
    """
    templates = group.templates.visible_to(bike.athlete)
    part_ids = {t.id for t in templates.filter(maintenance_kind=MaintenanceKind.PART)}
    consumable_ids = {
        t.id for t in templates.filter(maintenance_kind=MaintenanceKind.CONSUMABLE)
    }
    for item in data.get("parts", []):
        if item["template_id"] not in part_ids:
            return f"Template {item['template_id']} gehört nicht als Verschleißteil zu '{group.name}'."

        existing_slot_id = item.get("existing_slot_id")
        reuse_component_id = item.get("reuse_component_id")
        if existing_slot_id and reuse_component_id:
            return "existing_slot_id und reuse_component_id schließen sich aus."

        if existing_slot_id:
            slot = ComponentSlot.objects.filter(
                pk=existing_slot_id,
                bike=bike,
                assembly__isnull=True,
                template_id=item["template_id"],
            ).first()
            if slot is None:
                return (
                    f"Vorhandene Komponente {existing_slot_id} wurde nicht gefunden — "
                    "entweder nicht mehr ungruppiert oder falsches Template."
                )
            if slot.mounted_component is None:
                return f"Vorhandene Komponente {existing_slot_id} hat kein montiertes Teil."

        if reuse_component_id:
            component = (
                Component.objects.filter(
                    pk=reuse_component_id,
                    slot__bike=bike,
                    slot__template_id=item["template_id"],
                )
                .exclude(status=ComponentStatus.MOUNTED)
                .first()
            )
            if component is None:
                return (
                    f"Ausgebautes Teil {reuse_component_id} wurde nicht gefunden — "
                    "entweder inzwischen montiert oder falsches Template."
                )
    for item in data.get("intervals", []):
        if item["template_id"] not in consumable_ids:
            return f"Template {item['template_id']} gehört nicht als Verbrauchsmaterial zu '{group.name}'."
    return None


class BikeInstalledAtView(AthleteMixin, APIView):
    """
    POST /api/maintenance/bikes/{bike_id}/installed-at/
    Body: { "installed_at": "YYYY-MM-DD", "assembly_id"?: 12 }

    Setzt das Einbaudatum für **alle montierten Teile** auf einmal — wahlweise
    für alles, was am Rad ist, oder nur für eine Baugruppe.

    Der Anlass: wer ein Bike anlegt, dessen Fahrten schon in der App sind, hat
    beim Anlegen meist das heutige Datum stehen und korrigiert danach jedes Teil
    einzeln. Das sind bei einem Rennrad schnell zwölf Dialoge.
    """

    def post(self, request, bike_id):
        bike = get_object_or_404(Bike, pk=bike_id, athlete=self.get_athlete())

        body = BikeInstalledAtRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        installed_at = body.validated_data["installed_at"]
        assembly_id = body.validated_data.get("assembly_id")

        assembly = None
        if assembly_id is not None:
            assembly = get_object_or_404(BikeAssembly, pk=assembly_id, bike=bike)

        components, assemblies = lifecycle.set_installed_on_bulk(
            bike, installed_at, assembly
        )
        return Response(
            {
                "installed_at": installed_at,
                "components_updated": components,
                "assemblies_updated": assemblies,
            }
        )


def _group_templates_prefetch(athlete: StravaProfile) -> Prefetch:
    """Der Katalog zeigt jedem nur die gemeinsamen und die eigenen Vorlagen."""
    return Prefetch(
        "templates",
        queryset=ComponentTemplate.objects.visible_to(athlete).select_related("group"),
    )


class ComponentGroupListView(AthleteMixin, generics.ListAPIView):
    """
    GET /api/maintenance/groups/?bike_type=gravel

    Katalog aller Baugruppen-Blueprints mit genesteten Templates (parts/
    consumables). Für den Neu-Bike-Stepper und den "Baugruppe hinzufügen"-Dialog.
    """

    serializer_class = ComponentGroupSerializer

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx["bike_type"] = self.request.query_params.get("bike_type")
        return ctx

    def get_queryset(self):
        qs = ComponentGroup.objects.prefetch_related(
            _group_templates_prefetch(self.get_athlete())
        )
        bike_type = self.request.query_params.get("bike_type")
        if bike_type:
            qs = [g for g in qs if g.applies_to(bike_type)]
        return qs


def _assembly_prefetch(athlete: StravaProfile) -> tuple:
    return (
        Prefetch("group__templates", queryset=_visible_templates(athlete)),
        "slots__template__group",
        "slots__components__checks",
        "slots__components__periods",
        "intervals__logs",
        "periods",
        "linked_assemblies",
        "linked_components",
    )


def _visible_templates(athlete: StravaProfile):
    return ComponentTemplate.objects.visible_to(athlete).select_related("group")


class BikeAssemblyListView(AthleteMixin, APIView):
    """
    GET  /api/maintenance/bikes/{bike_id}/assemblies/
    POST /api/maintenance/bikes/{bike_id}/assemblies/

    GET liefert in einem Rutsch alles, was die Bike-Seite braucht: die Baugruppen am
    Rad (`assemblies`, inkl. Plätze, Teile und Intervalle), die geparkten Alternativen
    (`parked_assemblies`), die ausgemusterten (`retired_assemblies`), die noch nicht
    zugeordneten Alt-Plätze (`ungrouped_slots`), die Teile im Regal
    (`spare_components`) und die anlegbaren Katalog-Gruppen (`available_groups`). Ein
    Bereich mit vorhandener Instanz fehlt dort: dort ergänzt man Teile, statt eine
    zweite Bremse anzulegen. Ein Laufrad bleibt wählbar (`has_active_instance`), weil
    ein zweiter Satz der Normalfall ist; POST parkt ihn dann statt den ersten zu
    verdrängen.

    POST legt eine neue Baugruppe komplett an (ein Dialog = eine Baugruppe).
    """

    def get_bike(self) -> Bike:
        return get_object_or_404(
            Bike, pk=self.kwargs["bike_id"], athlete=self.get_athlete()
        )

    def get(self, request, bike_id):
        bike = self.get_bike()
        athlete = self.get_athlete()
        # Ein gemeinsamer Context, damit die Fahrten nur einmal geladen werden.
        context: dict = {}

        assemblies = (
            BikeAssembly.objects.filter(bike=bike)
            .select_related("group")
            .prefetch_related(*_assembly_prefetch(athlete))
        )

        def serialized(queryset):
            return BikeAssemblySerializer(queryset, many=True, context=context).data

        ungrouped = (
            ComponentSlot.objects.filter(bike=bike, assembly__isnull=True)
            .select_related("template__group", "bike")
            .prefetch_related("components__checks", "components__periods")
        )

        active_data = serialized(assemblies.active())
        used_group_ids = set(assemblies.active().values_list("group_id", flat=True))
        taken_areas = set(
            assemblies.exclude(status=AssemblyStatus.RETIRED)
            .filter(group__kind=GroupKind.AREA)
            .values_list("group_id", flat=True)
        )
        available = [
            g
            for g in ComponentGroup.objects.prefetch_related(
                _group_templates_prefetch(athlete)
            )
            if g.applies_to(bike.bike_type) and g.pk not in taken_areas
        ]

        return Response(
            {
                "assemblies": active_data,
                "parked_assemblies": serialized(assemblies.parked()),
                "retired_assemblies": serialized(assemblies.retired()),
                "ungrouped_slots": ComponentSlotSerializer(
                    ungrouped, many=True, context=context
                ).data,
                "spare_components": SpareComponentSerializer(
                    _spare_components_for_bike(bike), many=True, context=context
                ).data,
                "available_groups": ComponentGroupSerializer(
                    available,
                    many=True,
                    context={
                        "bike_type": bike.bike_type,
                        "used_group_ids": used_group_ids,
                    },
                ).data,
            }
        )

    def post(self, request, bike_id):
        bike = self.get_bike()
        body = AssemblyCreateRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data

        group_id = data.get("group_id")
        if not group_id:
            return Response(
                {"error": "group_id fehlt."}, status=status.HTTP_400_BAD_REQUEST
            )
        group = get_object_or_404(ComponentGroup, pk=group_id)

        if not group.applies_to(bike.bike_type):
            return Response(
                {
                    "error": f"Baugruppe '{group.name}' passt nicht zum Bike-Typ.",
                    "code": "bike_type_mismatch",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if group.kind == GroupKind.AREA:
            existing = (
                BikeAssembly.objects.filter(bike=bike, group=group)
                .exclude(status=AssemblyStatus.RETIRED)
                .first()
            )
            if existing is not None:
                return Response(
                    {
                        "error": f"'{group.name}' gibt es an diesem Rad schon. Ergänze "
                        "die Teile dort.",
                        "code": "area_exists",
                        "assembly_id": existing.pk,
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        err = _validate_assembly_items(bike, group, data)
        if err:
            return Response({"error": err}, status=status.HTTP_400_BAD_REQUEST)

        # Mehrere Instanzen derselben Gruppe sind erlaubt (Sommer-/Winter-LRS),
        # aber immer nur eine ist aufgezogen. Ohne explizites `activate` wird die
        # neue Instanz geparkt angelegt, solange die Gruppe belegt ist — die
        # bestehende soll nicht ungefragt verdrängt werden.
        current = lifecycle.active_sibling(bike, group)
        requested = data.get("activate")
        activate = (current is None) if requested is None else bool(requested)

        with transaction.atomic():
            assembly = _build_assembly_from_request(
                bike, group, data, activate=activate and current is None
            )
            if data.get("linked_assembly_ids"):
                lifecycle.set_links(assembly, data["linked_assembly_ids"], [])
            if activate and current is not None:
                lifecycle.activate_assembly(assembly, on=data.get("installed_at"))

        return Response(
            BikeAssemblySerializer(BikeAssembly.objects.get(pk=assembly.pk)).data,
            status=status.HTTP_201_CREATED,
        )


class BikeAssemblyDetailView(AthleteMixin, generics.RetrieveUpdateDestroyAPIView):
    """
    GET    /api/maintenance/assemblies/{pk}/
    PATCH  /api/maintenance/assemblies/{pk}/   → name
    DELETE /api/maintenance/assemblies/{pk}/?confirm=true

    Der Zustand (`status`) ist hier bewusst **nicht** schreibbar: ein Wechsel
    muss über activate/retire/restore laufen, sonst stimmten die Nutzungszeiträume
    nicht mehr.

    DELETE löscht endgültig, samt aller Teile und ihres Verlaufs — wer die Geschichte
    behalten will, mustert aus. Deshalb die Pflicht-Bestätigung: vorher löste DELETE
    nur die Gruppierung auf und hängte dabei auch die Teile eines Laufrads aus dem
    Keller als "am Rad" zurück.
    """

    serializer_class = BikeAssemblySerializer
    http_method_names = ["get", "patch", "delete", "head", "options"]

    def get_queryset(self):
        athlete = self.get_athlete()
        return (
            BikeAssembly.objects.filter(bike__athlete=athlete)
            .select_related("group", "bike")
            .prefetch_related(*_assembly_prefetch(athlete))
        )

    def destroy(self, request, *args, **kwargs):
        assembly = self.get_object()
        parts = Component.objects.filter(slot__assembly=assembly).count()
        if parts and request.query_params.get("confirm") != "true":
            return _confirm_required(
                f"Damit verschwinden {parts} Teil(e) samt ihres Verlaufs. Zum "
                "Aufbewahren der Geschichte stattdessen ausmustern.",
                parts,
            )
        assembly.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AssemblyAddItemView(AthleteMixin, APIView):
    """
    POST /api/maintenance/assemblies/{pk}/items/
    Body: { "template_id": 19, "brand"?, "model_name"?, "installed_at"?,
            "custom_warn_km"?, "interval_km"?, "interval_days"? }

    Ergaenzt eine bestehende Baugruppe um ein einzelnes Element — das Gegenstueck
    zum Assistenten, der nur beim Anlegen greift. Vorher liess sich eine
    vergessene Kassette nur nachtragen, indem man die ganze Baugruppe neu anlegte.

    Ob ein `ComponentSlot` (+ `Component`) oder ein `MaintenanceInterval`
    entsteht, entscheidet `template.maintenance_kind` — dieselbe Regel wie beim
    Anlegen, damit Verbrauchsmaterial nicht als Verschleissteil landet. Das
    Template muss zur Gruppe gehoeren; geprueft wird serverseitig, nicht im
    Client.
    """

    def post(self, request, pk):
        athlete = self.get_athlete()
        assembly = get_object_or_404(
            BikeAssembly.objects.select_related("group", "bike"),
            pk=pk,
            bike__athlete=athlete,
        )
        bike = assembly.bike

        body = AssemblyAddItemRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data

        if assembly.is_retired:
            return Response(
                {
                    "error": f"'{assembly.display_name}' ist ausgemustert.",
                    "code": "assembly_retired",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        template = (
            ComponentTemplate.objects.visible_to(athlete)
            .filter(pk=data["template_id"], group=assembly.group)
            .first()
        )
        if template is None:
            return Response(
                {
                    "error": "Dieses Element gehört nicht zu "
                    f"'{assembly.group.name}'.",
                    "code": "template_not_in_group",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        installed_at = data.get("installed_at") or ride_clock_now().date()

        if template.maintenance_kind == MaintenanceKind.CONSUMABLE:
            if MaintenanceInterval.objects.filter(
                assembly=assembly, template=template
            ).exists():
                return Response(
                    {
                        "error": f"'{template.name}' ist in dieser Baugruppe schon angelegt.",
                        "code": "already_present",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            interval = MaintenanceInterval.objects.create(
                bike=bike,
                assembly=assembly,
                template=template,
                kind=_interval_kind_for_template(template),
                label=template.name,
                interval_km=data.get("interval_km") or template.warn_km,
                interval_days=data.get("interval_days") or template.warn_days,
                last_done_at=installed_at,
                km_baseline_at=moment_for(installed_at),
            )
            return Response(
                MaintenanceIntervalSerializer(interval).data,
                status=status.HTTP_201_CREATED,
            )

        if ComponentSlot.objects.filter(assembly=assembly, template=template).exists():
            return Response(
                {
                    "error": f"'{template.name}' ist in dieser Baugruppe schon angelegt.",
                    "code": "already_present",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            slot = ComponentSlot.objects.create(
                bike=bike, assembly=assembly, template=template
            )
            component = lifecycle.add_component(
                slot,
                on=data.get("installed_at"),
                brand=data.get("brand", ""),
                model_name=data.get("model_name", ""),
                custom_warn_km=data.get("custom_warn_km"),
            )

        return Response(
            ComponentSerializer(component).data, status=status.HTTP_201_CREATED
        )


def _plan_data(plan: lifecycle.ActivationPlan) -> dict:
    """Der Wechsel in Worten — für die Vorschau und die Meldung danach."""
    return {
        "activate": [
            {"id": a.pk, "name": a.display_name, "group_name": a.group.name}
            for a in plan.activate
        ],
        "park": [
            {"id": a.pk, "name": a.display_name, "group_name": a.group.name}
            for a in plan.park
        ],
        "mount": [
            {
                "component_id": m.component.pk,
                "label": m.component.label,
                "slot_name": m.component.slot.display_name,
                "replaces": m.replaces.label if m.replaces else None,
            }
            for m in plan.mount
        ],
        "unpaired": [
            {
                "component_id": component.pk,
                "label": component.label,
                "slot_name": component.slot.display_name,
                "from_assembly": assembly.display_name,
            }
            for component, assembly in plan.unpaired
        ],
    }


class AssemblyActionView(AthleteMixin, APIView):
    """Gemeinsamer Rahmen der Aktionen auf einer Baugruppe."""

    def get_assembly(self, pk: int) -> BikeAssembly:
        return get_object_or_404(
            BikeAssembly.objects.select_related("group", "bike"),
            pk=pk,
            bike__athlete=self.get_athlete(),
        )

    def serialized(self, pk: int) -> dict:
        return dict(BikeAssemblySerializer(self.get_assembly(pk)).data)

    def action_body(self, request) -> dict:
        body = AssemblyActionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        return body.validated_data


class AssemblyActivateView(AssemblyActionView):
    """
    GET  /api/maintenance/assemblies/{pk}/activate/ → was alles mitwechseln würde
    POST /api/maintenance/assemblies/{pk}/activate/ → Body: { "on"?: "YYYY-MM-DD" }

    Zieht eine geparkte Baugruppe auf (Carbon-Laufrad statt Alu). Das gekoppelte andere
    Laufrad und die gekoppelten Teile (Bremsbeläge) wechseln mit, der bisher aufgezogene
    Satz wird geparkt und behält seine Teile. Die Vorschau nutzt dieselbe Rechnung wie
    die Ausführung, kann also nichts anderes versprechen, als dann passiert.
    """

    def get(self, request, pk):
        return Response(_plan_data(lifecycle.plan_activation(self.get_assembly(pk))))

    def post(self, request, pk):
        assembly = self.get_assembly(pk)
        on = self.action_body(request).get("on")
        plan = lifecycle.activate_assembly(assembly, on=on)
        return Response({**self.serialized(pk), "plan": _plan_data(plan)})


class AssemblyRetireView(AssemblyActionView):
    """
    POST /api/maintenance/assemblies/{pk}/retire/
    Body: { "on"?: "YYYY-MM-DD", "with_linked"?: true }

    Mustert eine Baugruppe aus (verkauft/entsorgt), mit `with_linked` den ganzen Satz.
    Die montierten Teile werden mit ausgemustert, die Instanz bleibt als Historie
    erhalten.
    """

    def post(self, request, pk):
        body = self.action_body(request)
        lifecycle.retire_assembly(
            self.get_assembly(pk), on=body.get("on"), with_linked=body["with_linked"]
        )
        return Response(self.serialized(pk))


class AssemblyRestoreView(AssemblyActionView):
    """POST /api/maintenance/assemblies/{pk}/restore/ — holt sie aus der Historie zurück."""

    def post(self, request, pk):
        lifecycle.restore_assembly(self.get_assembly(pk))
        return Response(self.serialized(pk))


class AssemblyLinksView(AssemblyActionView):
    """
    PUT /api/maintenance/assemblies/{pk}/links/
    Body: { "assembly_ids": [8], "component_ids": [42] }

    Legt fest, was beim Aufziehen mitwechselt. Ersetzt die bisherige Auswahl.
    """

    def put(self, request, pk):
        body = AssemblyLinksSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        lifecycle.set_links(
            self.get_assembly(pk),
            body.validated_data["assembly_ids"],
            body.validated_data["component_ids"],
        )
        return Response(self.serialized(pk))


class MaintenanceIntervalListCreateView(AthleteMixin, APIView):
    """
    POST /api/maintenance/bikes/{bike_id}/intervals/  → Ad-hoc-Intervall anlegen
    """

    def post(self, request, bike_id):
        bike = get_object_or_404(Bike, pk=bike_id, athlete=self.get_athlete())
        body = MaintenanceIntervalCreateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data

        assembly = None
        if data.get("assembly"):
            assembly = get_object_or_404(BikeAssembly, pk=data["assembly"], bike=bike)

        last_done_at = data.get("last_done_at") or ride_clock_now().date()
        interval = MaintenanceInterval.objects.create(
            bike=bike,
            assembly=assembly,
            kind=data.get("kind", MaintenanceIntervalKind.CUSTOM),
            label=data["label"],
            interval_km=data.get("interval_km"),
            interval_days=data.get("interval_days"),
            last_done_at=last_done_at,
            km_baseline_at=moment_for(last_done_at),
            notes=data.get("notes", ""),
        )
        return Response(
            MaintenanceIntervalSerializer(interval).data,
            status=status.HTTP_201_CREATED,
        )


class MaintenanceIntervalDetailView(
    AthleteMixin, generics.RetrieveUpdateDestroyAPIView
):
    """
    GET    /api/maintenance/intervals/{pk}/
    PATCH  /api/maintenance/intervals/{pk}/
    DELETE /api/maintenance/intervals/{pk}/
    """

    serializer_class = MaintenanceIntervalSerializer
    http_method_names = ["get", "patch", "delete", "head", "options"]

    def get_queryset(self):
        return (
            MaintenanceInterval.objects.filter(bike__athlete=self.get_athlete())
            .select_related("bike", "assembly")
            .prefetch_related("logs", "assembly__periods")
        )


class MaintenanceIntervalLogView(AthleteMixin, APIView):
    """
    POST   /api/maintenance/intervals/{pk}/log/  Body: { "done_at"?, "note"? }
    DELETE /api/maintenance/intervals/{pk}/log/  → nimmt den letzten Eintrag zurück

    "Erledigt / Erneuert": setzt die km-/Tage-Baseline zurück und hängt einen
    MaintenanceLog an.
    """

    def get_interval(self, pk: int) -> MaintenanceInterval:
        return get_object_or_404(
            MaintenanceInterval.objects.select_related("assembly"),
            pk=pk,
            bike__athlete=self.get_athlete(),
        )

    def post(self, request, pk):
        interval = self.get_interval(pk)
        body = MaintenanceIntervalLogRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        lifecycle.log_interval_done(
            interval, done_on=data.get("done_at"), note=data.get("note", "")
        )
        return Response(MaintenanceIntervalSerializer(interval).data)

    def delete(self, request, pk):
        interval = self.get_interval(pk)
        lifecycle.undo_interval_log(interval)
        return Response(MaintenanceIntervalSerializer(interval).data)


# ── "Kudo" — KI-Assistent fuers Bike-Anlegen ─────────────────────────────────
# Beide Endpoints liefern 503 statt eines Fehlers, wenn keine KI verfuegbar ist —
# das Frontend faellt dann auf die manuelle Einrichtung im Stepper zurueck, die
# unveraendert funktioniert.

AI_UNAVAILABLE_RESPONSE = {
    "error": "Kudo ist gerade nicht erreichbar. Du kannst dein Bike weiterhin manuell einrichten.",
    "code": "ai_unavailable",
}


class AssistantModelsView(AthleteMixin, APIView):
    """
    POST /api/maintenance/assistant/models/

    Schritt 1 von Kudo: Hersteller (+ optional Baujahr/Bike-Typ) rein, Liste
    plausibler Modelle raus, aus der der Nutzer seins auswaehlt.
    """

    def post(self, request):
        if not has_ai_consent(self.get_athlete()):
            return ai_consent_required_response()

        body = AssistantModelsRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data

        models = suggest_models(
            data["manufacturer"], data.get("year"), data.get("bike_type") or "other"
        )
        if models is None:
            return Response(
                AI_UNAVAILABLE_RESPONSE, status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        return Response({"models": models})


class AssistantSetupView(AthleteMixin, APIView):
    """
    POST /api/maintenance/bikes/{bike_id}/assistant/setup/

    Schritt 2 von Kudo: gewaehltes Modell rein, Vorbelegung fuer den Setup-Stepper
    raus. Legt selbst NICHTS an — der Nutzer laeuft danach den normalen Stepper
    durch und kann jede Zeile korrigieren oder abwaehlen.

    Die zurueckgegebenen `template_id`s sind serverseitig gegen den Katalog geprueft
    (siehe bike_assistant._filter_to_catalog), erfundene IDs kommen hier nicht an.
    """

    def post(self, request, bike_id):
        bike = get_object_or_404(Bike, pk=bike_id, athlete=self.get_athlete())
        if not has_ai_consent(bike.athlete):
            return ai_consent_required_response()

        body = AssistantSetupRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data

        suggestion = suggest_setup(
            bike,
            data["manufacturer"],
            data["model"],
            data.get("year"),
            spec=data.get("spec", ""),
        )
        if suggestion is None:
            return Response(
                AI_UNAVAILABLE_RESPONSE, status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        return Response(suggestion)
