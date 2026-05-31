from django.urls import path
from .views import (
    BikeListView,
    BikeDetailView,
    ComponentTemplateListView,
    ComponentTemplateDetailView,
    ComponentSlotListView,
    ComponentSlotDetailView,
    SlotMountView,
    SlotUnmountView,
    ComponentListView,
    ComponentDetailView,
)

urlpatterns = [
    path("maintenance/bikes/", BikeListView.as_view(), name="bike-list"),
    path("maintenance/bikes/<int:pk>/", BikeDetailView.as_view(), name="bike-detail"),
    path(
        "maintenance/bikes/<int:bike_id>/slots/",
        ComponentSlotListView.as_view(),
        name="slot-list",
    ),
    path(
        "maintenance/slots/<int:pk>/",
        ComponentSlotDetailView.as_view(),
        name="slot-detail",
    ),
    path(
        "maintenance/slots/<int:pk>/mount/", SlotMountView.as_view(), name="slot-mount"
    ),
    path(
        "maintenance/slots/<int:pk>/unmount/",
        SlotUnmountView.as_view(),
        name="slot-unmount",
    ),
    path(
        "maintenance/slots/<int:slot_id>/components/",
        ComponentListView.as_view(),
        name="component-list",
    ),
    path(
        "maintenance/components/<int:pk>/",
        ComponentDetailView.as_view(),
        name="component-detail",
    ),
    path(
        "maintenance/templates/",
        ComponentTemplateListView.as_view(),
        name="template-list",
    ),
    path(
        "maintenance/templates/<int:pk>/",
        ComponentTemplateDetailView.as_view(),
        name="template-detail",
    ),
]
