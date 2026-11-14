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
]
