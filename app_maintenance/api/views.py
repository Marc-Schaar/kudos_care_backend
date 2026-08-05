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
    Bike,
    ComponentTemplate,
    ComponentSlot,
    Component,
    ComponentCheck,
)
from .serializers import (
    BikeSerializer,
    BikeListSerializer,
    ComponentTemplateSerializer,
    ComponentSlotSerializer,
    ComponentSlotListSerializer,
    ComponentSerializer,
    ComponentCheckCreateSerializer,
    compute_wear,
)
from .services import (
    build_weather_explanation_prompt,
    build_check_instructions_prompt,
    build_bike_condition_report_prompt,
    bike_condition_report_is_stale,
)
from .ai_providers import get_ai_provider
import logging
logger = logging.getLogger('my_app_debug')


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
        logger.debug(f"DEBUG: Suche Fahrräder für Profile ID: {profile.id} (Strava-ID: {profile.strava_athlete_id})")
        
        all_bikes = Bike.objects.all()
        for b in all_bikes:
            logger.debug(f"DEBUG: Bike gefunden: {b.name}, verknüpft mit Profile ID: {getattr(b, 'athlete_id', 'None')}")
            
        return Bike.objects.filter(athlete=profile).prefetch_related("slots__template", "slots__components", "rides")
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
            "slots__template", "slots__components", "rides"
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
            or component.weather_wear_explanation_generated_at < component.weather_wear_computed_at
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
        explanation = get_ai_provider().generate_text(system_prompt, user_prompt)

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
            update_fields=["weather_wear_explanation", "weather_wear_explanation_generated_at"]
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
        instructions = get_ai_provider().generate_text(system_prompt, user_prompt)

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

        slots = bike.slots.select_related("template").prefetch_related("components")
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
        if bike.condition_report and not force_refresh and not bike_condition_report_is_stale(bike):
            return Response(
                {
                    "report": bike.condition_report,
                    "generated_at": bike.condition_report_generated_at,
                    "cached": True,
                }
            )

        system_prompt, user_prompt = build_bike_condition_report_prompt(bike, component_summaries)
        report = get_ai_provider().generate_text(system_prompt, user_prompt)

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
