import datetime

from django.shortcuts import get_object_or_404
from django.db import transaction
from django.utils import timezone

from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from app_auth.models import StravaProfile
from app_maintenance.models import (
    AssemblyUsagePeriod,
    Bike,
    BikeAssembly,
    ComponentGroup,
    ComponentTemplate,
    ComponentSlot,
    Component,
    ComponentCheck,
    MaintenanceInterval,
    MaintenanceIntervalKind,
    MaintenanceKind,
    MaintenanceLog,
)
from .serializers import (
    BikeSerializer,
    BikeListSerializer,
    BikeAssemblySerializer,
    ComponentGroupSerializer,
    ComponentTemplateSerializer,
    ComponentSlotSerializer,
    ComponentSlotListSerializer,
    ComponentSerializer,
    SpareComponentSerializer,
    ComponentCheckCreateSerializer,
    AssemblyCreateRequestSerializer,
    MaintenanceIntervalSerializer,
    MaintenanceIntervalCreateSerializer,
    MaintenanceIntervalLogRequestSerializer,
    QuickChangeRequestSerializer,
    AssistantModelsRequestSerializer,
    AssistantSetupRequestSerializer,
    compute_wear,
)
from .services import (
    build_weather_explanation_prompt,
    build_check_instructions_prompt,
    build_bike_condition_report_prompt,
    bike_condition_report_is_stale,
)
from .ai_providers import generate_reviewed_text
from .bike_assistant import suggest_models, suggest_setup
import logging

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


class BikeListView(AthleteMixin, generics.ListCreateAPIView):
    """
    GET  /api/maintenance/bikes/   → alle Bikes des Users
    POST /api/maintenance/bikes/   → neues Bike anlegen
    """

    def get_queryset(self):
        profile = self.get_athlete()
        logger.debug(
            f"DEBUG: Suche Fahrräder für Profile ID: {profile.id} (Strava-ID: {profile.strava_athlete_id})"
        )

        all_bikes = Bike.objects.all()
        for b in all_bikes:
            logger.debug(
                f"DEBUG: Bike gefunden: {b.name}, verknüpft mit Profile ID: {getattr(b, 'athlete_id', 'None')}"
            )

        return Bike.objects.filter(athlete=profile).prefetch_related(
            "slots__template",
            "slots__components",
            "slots__assembly__periods",
            "rides",
        )
        # logging.debug(f"Fetching bikes for athlete: {self.get_athlete()}")
        # logger.debug(f" Bikes: {Bike.objects.filter(athlete=self.get_athlete())}")
        # return Bike.objects.filter(athlete=self.get_athlete()).prefetch_related(
        #     "slots__template", "slots__components", "rides"
        # )

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
        return Bike.objects.filter(athlete=self.get_athlete()).prefetch_related(
            "slots__template",
            "slots__components",
            "slots__assembly__periods",
            "rides",
            "intervals__logs",
        )


class BikeDistanceAtDateView(AthleteMixin, APIView):
    """
    GET /api/maintenance/bikes/{bike_id}/distance-at/?date=YYYY-MM-DD

    Liefert den km-Stand des Bikes zum angegebenen Datum (Summe der
    Ride-Distanzen bis inkl. diesem Tag; Fahrten mit einem anderen Bike
    zählen nicht mit). Basis für die automatische Vorbefüllung von
    Component.distance_at_install anhand des Einbaudatums.
    """

    def get(self, request, bike_id):
        bike = get_object_or_404(Bike, pk=bike_id, athlete=self.get_athlete())
        date_param = request.query_params.get("date")
        if not date_param:
            return Response(
                {"error": "date fehlt (Format: YYYY-MM-DD)."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            as_of = datetime.date.fromisoformat(date_param)
        except ValueError:
            return Response(
                {"error": "date muss im Format YYYY-MM-DD sein."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response({"distance_km": bike.distance_km_up_to(as_of)})


class ComponentTemplateListView(AthleteMixin, generics.ListCreateAPIView):
    """
    GET  /api/maintenance/templates/?bike_type=mtb&category=brakes
    POST /api/maintenance/templates/   → eigene Vorlage anlegen
    """

    serializer_class = ComponentTemplateSerializer

    def get_queryset(self):
        qs = ComponentTemplate.objects.all()
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
        serializer.save(is_system=False)


class ComponentTemplateDetailView(AthleteMixin, generics.RetrieveUpdateDestroyAPIView):
    """
    GET    /api/maintenance/templates/{id}/
    PATCH  /api/maintenance/templates/{id}/   → nur eigene (is_system=False)
    DELETE /api/maintenance/templates/{id}/   → nur eigene
    """

    serializer_class = ComponentTemplateSerializer
    http_method_names = ["get", "patch", "delete", "head", "options"]

    def get_queryset(self):
        # GET darf alle sehen; PATCH/DELETE nur eigene
        if self.request.method == "GET":
            return ComponentTemplate.objects.all()
        return ComponentTemplate.objects.filter(is_system=False)


class ComponentSlotListView(AthleteMixin, generics.ListCreateAPIView):
    """
    GET  /api/maintenance/bikes/{bike_id}/slots/?category=brakes&warn=true
    POST /api/maintenance/bikes/{bike_id}/slots/
    """

    def get_bike(self):
        return get_object_or_404(
            Bike, pk=self.kwargs["bike_id"], athlete=self.get_athlete()
        )

    def get_queryset(self):
        qs = (
            ComponentSlot.objects.filter(bike=self.get_bike())
            .select_related("template")
            .prefetch_related("components")
        )
        category = self.request.query_params.get("category")
        if category:
            qs = qs.filter(template__category=category)
        return qs

    def get_serializer_class(self):
        return (
            ComponentSlotSerializer
            if self.request.method == "POST"
            else ComponentSlotListSerializer
        )

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        if request.query_params.get("warn") == "true":
            response.data = [
                s for s in response.data if s["warn_status"] in ("warn", "critical")
            ]
        return response

    def perform_create(self, serializer):
        serializer.save(bike=self.get_bike())


class ComponentSlotDetailView(AthleteMixin, generics.RetrieveUpdateDestroyAPIView):
    """
    GET    /api/maintenance/slots/{id}/
    PATCH  /api/maintenance/slots/{id}/
    DELETE /api/maintenance/slots/{id}/
    """

    serializer_class = ComponentSlotSerializer
    http_method_names = ["get", "patch", "delete", "head", "options"]

    def get_queryset(self):
        return (
            ComponentSlot.objects.filter(bike__athlete=self.get_athlete())
            .select_related("bike", "template")
            .prefetch_related("components")
        )


class SlotMountView(AthleteMixin, APIView):
    """
    POST /api/maintenance/slots/{pk}/mount/
    Body: { "component_id": 42 }

    Montiert eine vorhandene Komponente; baut die bisherige atomar aus.
    """

    def post(self, request, pk):
        slot = get_object_or_404(ComponentSlot, pk=pk, bike__athlete=self.get_athlete())
        component_id = request.data.get("component_id")
        if not component_id:
            return Response(
                {"error": "component_id fehlt."}, status=status.HTTP_400_BAD_REQUEST
            )
        new_comp = get_object_or_404(Component, pk=component_id, slot=slot)

        with transaction.atomic():
            Component.objects.filter(slot=slot, is_mounted=True).update(
                is_mounted=False,
                retired_at=datetime.date.today(),
            )
            new_comp.is_mounted = True
            new_comp.retired_at = None
            new_comp.save()

        return Response(ComponentSerializer(new_comp).data)


class SlotUnmountView(AthleteMixin, APIView):
    """
    POST /api/maintenance/slots/{pk}/unmount/

    Baut die aktuell montierte Komponente aus.
    """

    def post(self, request, pk):
        slot = get_object_or_404(ComponentSlot, pk=pk, bike__athlete=self.get_athlete())
        comp = slot.mounted_component
        if comp is None:
            return Response(
                {"error": "Keine montierte Komponente in diesem Slot."},
                status=status.HTTP_404_NOT_FOUND,
            )
        comp.is_mounted = False
        comp.retired_at = datetime.date.today()
        comp.save()
        return Response(ComponentSerializer(comp).data)


class SlotQuickChangeView(AthleteMixin, APIView):
    """
    GET  /api/maintenance/slots/{pk}/quick-change/
    POST /api/maintenance/slots/{pk}/quick-change/
    Body (POST): {
        "installed_at"?: "YYYY-MM-DD",
        "items": [{"slot_id": 12, "include": true, "brand"?: str, "model_name"?: str}, ...]
    }

    Baugruppen-Wechsel (z.B. "Laufrad vorne" wechseln → Reifen/Felge/Nabenlager/
    Speichen/Felgenband vorne gleich mit). `pk` ist einer der Slots der Gruppe
    (typischerweise der, den der User angeklickt hat) — GET liefert alle
    Geschwister-Slots derselben Baugruppe auf demselben Bike zur Vorschau, POST
    führt den Wechsel für die als `include=true` markierten Slots atomar aus,
    nach demselben Unmount/Mount-Muster wie SlotMountView. Welche Slots
    erlaubt sind, wird serverseitig aus slot.template.group hergeleitet, nicht
    aus dem Request übernommen.
    """

    def _sibling_slots(self, slot: ComponentSlot):
        group = slot.template.group
        if group is None:
            return None, None
        siblings = (
            ComponentSlot.objects.filter(bike=slot.bike, template__group=group)
            .select_related("template")
            .prefetch_related("components")
        )
        return group, siblings

    def get(self, request, pk):
        slot = get_object_or_404(
            ComponentSlot.objects.select_related("template", "bike"),
            pk=pk,
            bike__athlete=self.get_athlete(),
        )
        group, siblings = self._sibling_slots(slot)
        if group is None:
            return Response(
                {
                    "error": "Diese Komponente gehört zu keiner Baugruppe.",
                    "code": "no_group",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        items = []
        for sibling in siblings:
            comp = sibling.mounted_component
            items.append(
                {
                    "slot_id": sibling.id,
                    "display_name": sibling.display_name,
                    "preselected": True,
                    "mounted_component": (
                        {
                            "brand": comp.brand,
                            "model_name": comp.model_name,
                            "installed_at": comp.installed_at,
                        }
                        if comp is not None
                        else None
                    ),
                }
            )

        return Response({"group": {"id": group.id, "name": group.name}, "items": items})

    def post(self, request, pk):
        slot = get_object_or_404(
            ComponentSlot.objects.select_related("template", "bike"),
            pk=pk,
            bike__athlete=self.get_athlete(),
        )
        group, siblings = self._sibling_slots(slot)
        if group is None:
            return Response(
                {
                    "error": "Diese Komponente gehört zu keiner Baugruppe.",
                    "code": "no_group",
                },
                status=status.HTTP_404_NOT_FOUND,
            )
        allowed_slots = {s.id: s for s in siblings}

        body_serializer = QuickChangeRequestSerializer(data=request.data)
        body_serializer.is_valid(raise_exception=True)
        installed_at = (
            body_serializer.validated_data.get("installed_at") or datetime.date.today()
        )

        target_slots = []
        for item in body_serializer.validated_data["items"]:
            if not item["include"]:
                continue
            target_slot = allowed_slots.get(item["slot_id"])
            if target_slot is None:
                return Response(
                    {
                        "error": f"slot_id {item['slot_id']} gehört nicht zur Baugruppe dieses Bikes."
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            target_slots.append((target_slot, item))

        with transaction.atomic():
            bike_total_km = slot.bike.total_distance_km
            for target_slot, item in target_slots:
                Component.objects.filter(slot=target_slot, is_mounted=True).update(
                    is_mounted=False,
                    retired_at=datetime.date.today(),
                )
                new_comp = Component(
                    slot=target_slot,
                    brand=item["brand"],
                    model_name=item["model_name"],
                    installed_at=installed_at,
                    distance_at_install=bike_total_km,
                    is_mounted=True,
                )
                new_comp.save()

        changed_slots = (
            ComponentSlot.objects.filter(pk__in=[ts.id for ts, _ in target_slots])
            .select_related("template", "bike")
            .prefetch_related("components")
        )
        return Response(ComponentSlotSerializer(changed_slots, many=True).data)


# ── Components ────────────────────────────────────────────────────────────────


class ComponentListView(AthleteMixin, generics.ListCreateAPIView):
    """
    GET  /api/maintenance/slots/{slot_id}/components/
    POST /api/maintenance/slots/{slot_id}/components/
    """

    serializer_class = ComponentSerializer

    def get_slot(self):
        return get_object_or_404(
            ComponentSlot, pk=self.kwargs["slot_id"], bike__athlete=self.get_athlete()
        )

    def get_queryset(self):
        return self.get_slot().components.all()

    def perform_create(self, serializer):
        slot = self.get_slot()
        with transaction.atomic():
            if serializer.validated_data.get("is_mounted", True):
                Component.objects.filter(slot=slot, is_mounted=True).update(
                    is_mounted=False,
                    retired_at=datetime.date.today(),
                )
            serializer.save(slot=slot)


class ComponentDetailView(AthleteMixin, generics.RetrieveUpdateDestroyAPIView):
    """
    GET    /api/maintenance/components/{id}/
    PATCH  /api/maintenance/components/{id}/
    DELETE /api/maintenance/components/{id}/
    """

    serializer_class = ComponentSerializer
    http_method_names = ["get", "patch", "delete", "head", "options"]

    def get_queryset(self):
        return Component.objects.filter(
            slot__bike__athlete=self.get_athlete()
        ).select_related("slot__bike", "slot__template")


class ComponentCheckView(AthleteMixin, APIView):
    """
    POST /api/maintenance/components/{pk}/check/
    Body: { "condition_pct"?: 0-100, "snooze_km"?: float, "snooze_days"?: int, "note"?: str }

    Protokolliert eine Prüfung/Freigabe der Komponente ("Freigeben"). Der
    Warn-Status wird danach ab diesem Zeitpunkt neu berechnet — mit dem
    angegebenen Snooze-Intervall, falls keins angegeben wurde mit der
    normalen empfohlenen/individuellen Lebensdauer.
    """

    def post(self, request, pk):
        component = get_object_or_404(
            Component, pk=pk, slot__bike__athlete=self.get_athlete()
        )
        serializer = ComponentCheckCreateSerializer(
            data=request.data, context={"component": component}
        )
        serializer.is_valid(raise_exception=True)

        ComponentCheck.objects.create(
            component=component,
            checked_at=datetime.date.today(),
            checked_at_distance_km=component.slot.bike.total_distance_km,
            checked_at_weather_wear_km=component.weather_wear_km,
            **serializer.validated_data,
        )

        return Response(ComponentSerializer(component).data)


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

        wear = compute_wear(component, component.slot.bike.total_distance_km)
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

        wear = compute_wear(component, component.slot.bike.total_distance_km)

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
            .prefetch_related("components", "assembly__periods")
        )
        component_summaries = []
        for slot in slots:
            comp = slot.mounted_component
            if comp is None:
                continue
            wear = compute_wear(comp, bike.total_distance_km)
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


def _odometer_at(bike: Bike, day: datetime.date) -> float | None:
    """
    Km-Stand des Bikes an einem Tag — Baseline für eine Nutzungsperiode. Bei
    rückdatiertem Einbau ("den Satz fahre ich seit letztem Monat") wird der
    damalige Stand aus der Ride-Historie genommen, sonst zählte die Zeit davor
    nicht mit. Gleiche Quelle wie `total_distance_km` (Summe der Ride-Distanzen),
    die beiden Werte sind daher direkt vergleichbar.
    """
    total = bike.total_distance_km
    if total is None:
        return None
    if day >= datetime.date.today():
        return total
    return min(bike.distance_km_up_to(day), total)


def _park_assembly(assembly: BikeAssembly, on: datetime.date | None = None) -> None:
    """
    Baugruppe abziehen, ohne sie auszumustern: laufende Nutzungsperiode schließen,
    `is_active=False`, `retired_at` bleibt None. Die Komponenten bleiben
    `is_mounted=True` — sie sitzen ja weiter auf dem Laufradsatz, nur der Satz ist
    nicht mehr am Rad. Vor dem Parken wird der Wetter-Verschleiß ein letztes Mal
    final berechnet, danach ändert er sich nicht mehr.
    """
    from .services import WeatherWearService

    on = on or datetime.date.today()
    # Baugruppen aus der Zeit vor den Nutzungsperioden (bzw. aus seed_dev_data/
    # Admin) haben noch keine — die wird hier rückwirkend angelegt, sonst liesse
    # sich das Abziehen gar nicht festhalten und der Satz sammelte weiter km.
    period = assembly.ensure_open_period()
    if period is not None:
        period.close(on, assembly.bike.total_distance_km)

    assembly.is_active = False
    assembly.save(update_fields=["is_active", "updated_at"])

    for component in Component.objects.filter(
        slot__assembly=assembly, is_mounted=True
    ).select_related("slot__template", "slot__assembly"):
        try:
            WeatherWearService.recompute_component(component)
        except Exception:
            logger.exception(
                "Finale Wetter-Verschleiss-Berechnung fuer Komponente %s fehlgeschlagen.",
                component.pk,
            )


def _mount_assembly(assembly: BikeAssembly, on: datetime.date | None = None) -> None:
    """
    Baugruppe aufziehen: aktiv setzen und eine neue Nutzungsperiode öffnen. Der
    Aufrufer muss vorher sicherstellen, dass keine andere Instanz derselben
    `(bike, group)` mehr aktiv ist (siehe `BikeAssembly.clean()`).
    """
    on = on or datetime.date.today()
    assembly.is_active = True
    assembly.retired_at = None
    assembly.save(update_fields=["is_active", "retired_at", "updated_at"])

    if assembly.open_period() is None:
        AssemblyUsagePeriod.objects.create(
            assembly=assembly,
            started_at=on,
            started_distance_km=_odometer_at(assembly.bike, on),
        )


def _active_sibling(bike: Bike, group: ComponentGroup) -> BikeAssembly | None:
    """Die aktuell aufgezogene Instanz derselben Baugruppe an diesem Bike."""
    return (
        BikeAssembly.objects.filter(bike=bike, group=group, is_active=True)
        .select_related("bike", "group")
        .first()
    )


def _spare_components_by_template(bike: Bike) -> dict[int, Component]:
    """
    Je Template die zuletzt ausgebaute, noch nicht wiederverwendete Component
    dieses Bikes — Grundlage für den "vorhandene Komponente übernehmen"-
    Vorschlag bei ausgebauten Teilen (im Unterschied zu `ungrouped_slots`, die
    noch *montierte*, nur noch nicht gruppierte Alt-Teile abdecken). Ein Teil
    bleibt beim Ausbau immer an seinem alten Slot hängen (`is_mounted=False`),
    unabhängig davon, ob dessen Baugruppe noch aktiv/geparkt/ausgemustert ist —
    ein zurückgelegter Laufradsatz-Teil ist genauso ein Kandidat wie einer aus
    einer längst ausgemusterten Baugruppe.
    """
    spares = (
        Component.objects.filter(slot__bike=bike, is_mounted=False)
        .select_related("slot__template")
        .order_by("slot__template_id", "-retired_at", "-installed_at", "-id")
    )
    by_template: dict[int, Component] = {}
    for comp in spares:
        by_template.setdefault(comp.slot.template_id, comp)
    return by_template


def _build_assembly_from_request(
    bike: Bike, group: ComponentGroup, data: dict, activate: bool = True
) -> BikeAssembly:
    """
    Legt eine BikeAssembly + ihre Slots/Components + MaintenanceIntervals atomar an.
    Erwartet bereits validierte Daten (AssemblyCreateRequestSerializer) und
    vorab-geprüfte Template-Zugehörigkeit zur Gruppe. Wiederverwendet das
    Unmount-dann-Mount-Muster aus SlotMountView/SlotQuickChangeView.

    `activate=False` legt die Instanz geparkt an (zweiter Laufradsatz, der erst
    später aufgezogen wird) — dann gibt es auch noch keine Nutzungsperiode.

    Ein Part-Item mit `existing_slot_id` übernimmt eine bereits vorhandene,
    ungruppierte Component (samt Verlauf) statt eine neue anzulegen — dafür
    wird nur der Slot umgehängt (`slot.assembly = assembly`), nicht kopiert.
    `reuse_component_id` deckt den verwandten Fall eines bereits *ausgebauten*
    Teils ab (z.B. der zurückgelegte Laufradsatz aus dem Keller): die Component
    zieht in einen frisch angelegten Slot dieser Baugruppe um und wird wieder
    montiert (`is_mounted=True`, `retired_at`/`distance_at_retire`
    zurückgesetzt). Anders als bei `existing_slot_id` (durchgehend derselbe
    physische Einbau, nie ausgebaut) bleibt hier `installed_at`/
    `distance_at_install` unangetastet, aber der Periodenbeginn wird **nicht**
    darauf zurückdatiert — die Standzeit zwischen Ausbau und Wiedermontage soll
    ja gerade nicht als gefahrene km zählen (sonst würde km, die zwischenzeitlich
    ein anderer Laufradsatz gefahren ist, hier mitgezählt). Die Tage-Achse
    altert trotzdem seit dem ursprünglichen `installed_at` weiter, exakt wie bei
    einer geparkten statt ausgebauten Baugruppe — nur die km-Achse startet
    wieder bei 0. Für `existing_slot_id` gilt dagegen: damit der Verlauf des
    übernommenen Teils dabei nicht verloren geht (die Nutzungsperiode sonst
    erst ab heute zählen würde, siehe api/usage.py), wird der Periodenbeginn
    dort auf das früheste Einbaudatum/km unter den übernommenen Teilen
    zurückdatiert — neu angelegte Teile bleiben unangetastet beim gemeinsamen
    `installed_at`.
    """
    installed_at = data.get("installed_at") or datetime.date.today()
    # Bei rückdatiertem Einbau der damalige km-Stand, damit Baugruppen-Periode und
    # Komponenten-Baseline dieselbe Zahl benutzen und nicht auseinanderlaufen.
    bike_total_km = _odometer_at(bike, installed_at)

    with transaction.atomic():
        assembly = BikeAssembly(
            bike=bike,
            group=group,
            name=data.get("name", "") or "",
            installed_at=installed_at,
            is_active=activate,
        )
        assembly.save()

        allowed_parts = {
            t.id: t
            for t in group.templates.filter(maintenance_kind=MaintenanceKind.PART)
        }
        allowed_consumables = {
            t.id: t
            for t in group.templates.filter(maintenance_kind=MaintenanceKind.CONSUMABLE)
        }

        # Frühestes Einbaudatum/km unter den übernommenen Bestandsteilen —
        # bestimmt den Periodenbeginn (siehe Docstring oben).
        period_started_at = installed_at
        period_started_km = bike_total_km

        for item in data.get("parts", []):
            if not item["include"]:
                continue
            template = allowed_parts[item["template_id"]]

            existing_slot_id = item.get("existing_slot_id")
            if existing_slot_id:
                slot = ComponentSlot.objects.prefetch_related("components").get(
                    pk=existing_slot_id,
                    bike=bike,
                    assembly__isnull=True,
                    template=template,
                )
                slot.assembly = assembly
                slot.save(update_fields=["assembly"])

                mounted = slot.mounted_component
                if mounted is not None and mounted.installed_at is not None:
                    if mounted.installed_at < period_started_at:
                        period_started_at = mounted.installed_at
                    if mounted.distance_at_install is not None and (
                        period_started_km is None
                        or mounted.distance_at_install < period_started_km
                    ):
                        period_started_km = mounted.distance_at_install
                continue

            reuse_component_id = item.get("reuse_component_id")
            if reuse_component_id:
                component = Component.objects.select_for_update().get(
                    pk=reuse_component_id,
                    slot__bike=bike,
                    slot__template=template,
                    is_mounted=False,
                )
                slot = ComponentSlot.objects.create(
                    bike=bike, assembly=assembly, template=template
                )
                component.slot = slot
                component.is_mounted = True
                component.retired_at = None
                component.distance_at_retire = None
                component.save(
                    update_fields=[
                        "slot",
                        "is_mounted",
                        "retired_at",
                        "distance_at_retire",
                    ]
                )
                # Periodenbeginn bewusst NICHT zurückdatiert (siehe Docstring) —
                # die km-Achse beginnt für das wiedermontierte Teil neu bei 0,
                # nur die Tage-Achse zählt über sein `installed_at` weiter.
                continue

            slot, _ = ComponentSlot.objects.get_or_create(
                assembly=assembly, template=template, defaults={"bike": bike}
            )
            Component.objects.filter(slot=slot, is_mounted=True).update(
                is_mounted=False, retired_at=datetime.date.today()
            )
            Component(
                slot=slot,
                brand=item.get("brand", ""),
                model_name=item.get("model_name", ""),
                installed_at=installed_at,
                distance_at_install=bike_total_km,
                custom_warn_km=item.get("custom_warn_km"),
                custom_warn_days=item.get("custom_warn_days"),
                is_mounted=True,
            ).save()

        if activate:
            AssemblyUsagePeriod.objects.create(
                assembly=assembly,
                started_at=period_started_at,
                started_distance_km=period_started_km,
            )

        for item in data.get("intervals", []):
            if not item["include"]:
                continue
            template = allowed_consumables[item["template_id"]]
            MaintenanceInterval.objects.create(
                bike=bike,
                assembly=assembly,
                template=template,
                kind=_interval_kind_for_template(template),
                label=template.name,
                interval_km=item.get("interval_km") or template.warn_km,
                interval_days=item.get("interval_days") or template.warn_days,
                last_done_at=installed_at,
                last_done_distance_km=bike_total_km,
            )

    return assembly


def _validate_assembly_items(
    bike: Bike, group: ComponentGroup, data: dict
) -> str | None:
    """
    Prüft, dass alle referenzierten Templates zur Gruppe + richtigen Art gehören,
    und dass jede referenzierte `existing_slot_id` (vorhandene, montierte
    Komponente übernehmen) bzw. `reuse_component_id` (ausgebautes Teil
    reaktivieren) wirklich zu diesem Bike und Template passt.
    """
    part_ids = {
        t.id for t in group.templates.filter(maintenance_kind=MaintenanceKind.PART)
    }
    consumable_ids = {
        t.id
        for t in group.templates.filter(maintenance_kind=MaintenanceKind.CONSUMABLE)
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
            component = Component.objects.filter(
                pk=reuse_component_id,
                slot__bike=bike,
                slot__template_id=item["template_id"],
                is_mounted=False,
            ).first()
            if component is None:
                return (
                    f"Ausgebautes Teil {reuse_component_id} wurde nicht gefunden — "
                    "entweder inzwischen montiert oder falsches Template."
                )
    for item in data.get("intervals", []):
        if item["template_id"] not in consumable_ids:
            return f"Template {item['template_id']} gehört nicht als Verbrauchsmaterial zu '{group.name}'."
    return None


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
        qs = ComponentGroup.objects.prefetch_related("templates")
        bike_type = self.request.query_params.get("bike_type")
        if bike_type:
            qs = [g for g in qs if g.applies_to(bike_type)]
        return qs


class BikeAssemblyListView(AthleteMixin, APIView):
    """
    GET  /api/maintenance/bikes/{bike_id}/assemblies/
    POST /api/maintenance/bikes/{bike_id}/assemblies/

    GET liefert die aktiven Baugruppen des Bikes (inkl. Slots mit Wear +
    Intervall-Status), die geparkten Alternativen (`parked_assemblies` — z.B. der
    Winter-LRS im Keller), die noch nicht zugeordneten Alt-Slots
    (`ungrouped_slots`, nach Kategorie), die ausgebauten Ersatzteile
    (`spare_components`, für den "vorhandene Komponente übernehmen"-Vorschlag)
    und die zum Bike-Typ passenden Katalog-Gruppen (`available_groups`).
    `available_groups` enthält bewusst auch Gruppen mit bereits aktiver
    Instanz (`has_active_instance: true`) — ein zweiter Satz (Sommer-/
    Winter-LRS) soll sich anlegen lassen, POST parkt ihn dann automatisch statt
    die aktive Instanz zu verdrängen (siehe `_active_sibling` weiter unten).
    POST legt eine neue Baugruppe komplett an (ein Dialog = eine Baugruppe).
    """

    ASSEMBLY_PREFETCH = (
        "group__templates",
        "slots__template",
        "slots__components__checks",
        "intervals__logs",
        "periods",
    )

    def get_bike(self) -> Bike:
        return get_object_or_404(
            Bike, pk=self.kwargs["bike_id"], athlete=self.get_athlete()
        )

    def get(self, request, bike_id):
        bike = self.get_bike()
        context = {"bike_total_km": bike.total_distance_km}

        assemblies = (
            BikeAssembly.objects.filter(bike=bike, is_active=True)
            .select_related("group")
            .prefetch_related(*self.ASSEMBLY_PREFETCH)
        )
        assembly_data = BikeAssemblySerializer(
            assemblies, many=True, context=context
        ).data

        # Abgezogen, aber nicht entsorgt — die Alternativen, zwischen denen der
        # Wechsel-Dialog auswählen lässt.
        parked = (
            BikeAssembly.objects.filter(
                bike=bike, is_active=False, retired_at__isnull=True
            )
            .select_related("group")
            .prefetch_related(*self.ASSEMBLY_PREFETCH)
        )
        parked_data = BikeAssemblySerializer(
            parked, many=True, context=dict(context)
        ).data

        ungrouped = (
            ComponentSlot.objects.filter(bike=bike, assembly__isnull=True)
            .select_related("template")
            .prefetch_related("components__checks")
        )
        ungrouped_data = ComponentSlotListSerializer(
            ungrouped, many=True, context=dict(context)
        ).data

        spare_data = SpareComponentSerializer(
            list(_spare_components_by_template(bike).values()), many=True
        ).data

        used_group_ids = set(assemblies.values_list("group_id", flat=True))
        available = [
            g
            for g in ComponentGroup.objects.prefetch_related("templates")
            if g.applies_to(bike.bike_type)
        ]
        available_data = ComponentGroupSerializer(
            available,
            many=True,
            context={"bike_type": bike.bike_type, "used_group_ids": used_group_ids},
        ).data

        return Response(
            {
                "assemblies": assembly_data,
                "parked_assemblies": parked_data,
                "ungrouped_slots": ungrouped_data,
                "spare_components": spare_data,
                "available_groups": available_data,
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

        err = _validate_assembly_items(bike, group, data)
        if err:
            return Response({"error": err}, status=status.HTTP_400_BAD_REQUEST)

        # Mehrere Instanzen derselben Gruppe sind erlaubt (Sommer-/Winter-LRS),
        # aber immer nur eine ist aufgezogen. Ohne explizites `activate` wird die
        # neue Instanz geparkt angelegt, solange die Gruppe belegt ist — die
        # bestehende soll nicht ungefragt verdrängt werden.
        current = _active_sibling(bike, group)
        requested = data.get("activate")
        activate = (current is None) if requested is None else bool(requested)

        with transaction.atomic():
            if activate and current is not None:
                _park_assembly(current)
            assembly = _build_assembly_from_request(
                bike, group, data, activate=activate
            )

        return Response(
            BikeAssemblySerializer(
                assembly, context={"bike_total_km": bike.total_distance_km}
            ).data,
            status=status.HTTP_201_CREATED,
        )


class BikeAssemblyDetailView(AthleteMixin, generics.RetrieveUpdateDestroyAPIView):
    """
    GET    /api/maintenance/assemblies/{pk}/
    PATCH  /api/maintenance/assemblies/{pk}/   → name / installed_at / retired_at / is_active
    DELETE /api/maintenance/assemblies/{pk}/
    """

    serializer_class = BikeAssemblySerializer
    http_method_names = ["get", "patch", "delete", "head", "options"]

    def get_queryset(self):
        return (
            BikeAssembly.objects.filter(bike__athlete=self.get_athlete())
            .select_related("group", "bike")
            .prefetch_related(
                "group__templates",
                "slots__template",
                "slots__components__checks",
                "intervals__logs",
                "periods",
            )
        )


def _retire_assembly(assembly: BikeAssembly, on: datetime.date | None = None) -> None:
    """
    Baugruppe endgültig ausmustern (verkauft/entsorgt) — im Unterschied zum
    Parken werden hier auch die Komponenten ausgebaut. `distance_at_retire` hält
    den km-Stand fest, damit ein später erneuter Blick auf die Historie das Teil
    korrekt abschneiden kann (siehe api/usage.py).
    """
    on = on or datetime.date.today()
    period = assembly.ensure_open_period()
    if period is not None:
        period.close(on, assembly.bike.total_distance_km)

    Component.objects.filter(slot__assembly=assembly, is_mounted=True).update(
        is_mounted=False,
        retired_at=on,
        distance_at_retire=assembly.bike.total_distance_km,
    )
    assembly.is_active = False
    assembly.retired_at = on
    assembly.save(update_fields=["is_active", "retired_at", "updated_at"])


class AssemblyActivateView(AthleteMixin, APIView):
    """
    POST /api/maintenance/assemblies/{pk}/activate/

    Zieht eine geparkte Baugruppe auf (Winter-LRS montieren). Die bislang aktive
    Instanz derselben `(bike, group)` wird dabei geparkt — nicht ausgemustert:
    ihre Komponenten bleiben montiert, sie sammelt ab jetzt nur keine km und
    keinen Wetter-Verschleiß mehr (siehe AssemblyUsagePeriod).
    """

    def post(self, request, pk):
        assembly = get_object_or_404(
            BikeAssembly.objects.select_related("group", "bike"),
            pk=pk,
            bike__athlete=self.get_athlete(),
        )
        bike = assembly.bike

        if assembly.retired_at is not None:
            return Response(
                {
                    "error": f"'{assembly.display_name}' ist ausgemustert und kann "
                    "nicht wieder aufgezogen werden.",
                    "code": "retired",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if assembly.is_active:
            return Response(
                BikeAssemblySerializer(
                    assembly, context={"bike_total_km": bike.total_distance_km}
                ).data
            )

        with transaction.atomic():
            current = _active_sibling(bike, assembly.group)
            if current is not None:
                _park_assembly(current)
            _mount_assembly(assembly)

        # Bewusst kein Celery-Recompute: der abgezogene Satz wurde in
        # `_park_assembly()` final durchgerechnet, und der neu aufgezogene hat
        # seit seinem letzten Abziehen keine Fahrt mitgemacht — seine Zahlen
        # stehen also schon richtig. Der nächste Ride-Import rechnet ohnehin neu.
        return Response(
            BikeAssemblySerializer(
                assembly, context={"bike_total_km": bike.total_distance_km}
            ).data
        )


class AssemblyRetireView(AthleteMixin, APIView):
    """
    POST /api/maintenance/assemblies/{pk}/retire/

    Mustert eine Baugruppe endgültig aus (verkauft/entsorgt). Anders als beim
    Parken werden die Komponenten ausgebaut; die Instanz bleibt als Historie
    erhalten, taucht aber nicht mehr in der Wechsel-Auswahl auf.
    """

    def post(self, request, pk):
        assembly = get_object_or_404(
            BikeAssembly.objects.select_related("group", "bike"),
            pk=pk,
            bike__athlete=self.get_athlete(),
        )
        bike = assembly.bike

        with transaction.atomic():
            _retire_assembly(assembly)

        return Response(
            BikeAssemblySerializer(
                assembly, context={"bike_total_km": bike.total_distance_km}
            ).data
        )


class AssemblySwapView(AthleteMixin, APIView):
    """
    POST /api/maintenance/assemblies/{pk}/swap/

    "Teile erneuern": die alte Instanz wird ausgemustert (Komponenten ausgebaut),
    eine neue aktive Instanz derselben `group` wird mit frischen Komponenten/
    Intervallen angelegt. Body identisch zu POST /bikes/{id}/assemblies/
    (group_id optional, wird aus der alten Instanz übernommen).

    Nicht zu verwechseln mit `activate/`: dort wird zwischen zwei *bestehenden*
    Sätzen gewechselt, hier wird der alte Satz durch neue Teile ersetzt.
    """

    def post(self, request, pk):
        old = get_object_or_404(
            BikeAssembly.objects.select_related("group", "bike"),
            pk=pk,
            bike__athlete=self.get_athlete(),
        )
        bike = old.bike
        group = old.group

        body = AssemblyCreateRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data

        err = _validate_assembly_items(bike, group, data)
        if err:
            return Response({"error": err}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            _retire_assembly(old)
            new_assembly = _build_assembly_from_request(bike, group, data)

        return Response(
            BikeAssemblySerializer(
                new_assembly, context={"bike_total_km": bike.total_distance_km}
            ).data,
            status=status.HTTP_201_CREATED,
        )


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

        interval = MaintenanceInterval.objects.create(
            bike=bike,
            assembly=assembly,
            kind=data.get("kind", MaintenanceIntervalKind.CUSTOM),
            label=data["label"],
            interval_km=data.get("interval_km"),
            interval_days=data.get("interval_days"),
            last_done_at=data.get("last_done_at") or datetime.date.today(),
            last_done_distance_km=bike.total_distance_km,
            notes=data.get("notes", ""),
        )
        return Response(
            MaintenanceIntervalSerializer(
                interval, context={"bike_total_km": bike.total_distance_km}
            ).data,
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
            .select_related("bike")
            .prefetch_related("logs")
        )


class MaintenanceIntervalLogView(AthleteMixin, APIView):
    """
    POST /api/maintenance/intervals/{pk}/log/
    Body: { "done_at"?: "YYYY-MM-DD", "done_distance_km"?: float, "note"?: str }

    "Erledigt / Erneuert": setzt die km-/Tage-Baseline zurück und hängt einen
    MaintenanceLog an.
    """

    def post(self, request, pk):
        interval = get_object_or_404(
            MaintenanceInterval, pk=pk, bike__athlete=self.get_athlete()
        )
        body = MaintenanceIntervalLogRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data

        done_at = data.get("done_at") or datetime.date.today()
        done_km = data.get("done_distance_km")
        if done_km is None:
            done_km = interval.bike.total_distance_km

        with transaction.atomic():
            MaintenanceLog.objects.create(
                interval=interval,
                done_at=done_at,
                done_distance_km=done_km,
                note=data.get("note", ""),
            )
            interval.last_done_at = done_at
            interval.last_done_distance_km = done_km
            interval.save(
                update_fields=["last_done_at", "last_done_distance_km", "updated_at"]
            )

        return Response(
            MaintenanceIntervalSerializer(
                interval, context={"bike_total_km": interval.bike.total_distance_km}
            ).data
        )


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
        body = AssistantSetupRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data

        suggestion = suggest_setup(
            bike, data["manufacturer"], data["model"], data.get("year")
        )
        if suggestion is None:
            return Response(
                AI_UNAVAILABLE_RESPONSE, status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        return Response(suggestion)
