import datetime

from django.shortcuts import get_object_or_404
from django.db import transaction

from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from app_auth.models import StravaProfile
from app_maintenance.models import Bike, ComponentTemplate, ComponentSlot, Component
from .serializers import (
    BikeSerializer,
    BikeListSerializer,
    ComponentTemplateSerializer,
    ComponentSlotSerializer,
    ComponentSlotListSerializer,
    ComponentSerializer,
)


class AthleteMixin:
    """
    Stellt get_athlete() bereit und schränkt QuerySets automatisch
    auf den eingeloggten User ein.
    """

    permission_classes = [IsAuthenticated]

    def get_athlete(self) -> StravaProfile:
        athlete_id = self.request.session.get("strava_athlete_id")
        return get_object_or_404(StravaProfile, strava_athlete_id=athlete_id)


class BikeListView(AthleteMixin, generics.ListCreateAPIView):
    """
    GET  /api/maintenance/bikes/   → alle Bikes des Users
    POST /api/maintenance/bikes/   → neues Bike anlegen
    """

    def get_queryset(self):
        return Bike.objects.filter(athlete=self.get_athlete()).prefetch_related(
            "slots__template", "slots__components", "rides"
        )

    def get_serializer_class(self):
        return BikeSerializer if self.request.method == "POST" else BikeListSerializer

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
