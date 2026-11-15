from django.urls import path

from . import views

urlpatterns = [
    path("upload/", views.UploadView.as_view(), name="radcomputer-upload"),
    path(
        "upload/<int:upload_id>/",
        views.UploadStatusView.as_view(),
        name="radcomputer-upload-status",
    ),
    path("routes/", views.RouteListView.as_view(), name="radcomputer-routes"),
    path(
        "routes/<str:route_id>/gpx/",
        views.RouteGpxView.as_view(),
        name="radcomputer-route-gpx",
    ),
    path("wind/", views.WindView.as_view(), name="radcomputer-wind"),
    path("settings/", views.SettingsView.as_view(), name="radcomputer-settings"),
    path("places/", views.SavedPlaceListView.as_view(), name="radcomputer-places"),
    path(
        "places/<int:place_id>/",
        views.SavedPlaceDetailView.as_view(),
        name="radcomputer-place-detail",
    ),
]
