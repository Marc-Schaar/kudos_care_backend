from django.contrib import admin
from django.urls import include, path

admin.site.site_header = "Kudos Care Administration"
admin.site.site_title = "Kudos Care Admin"
admin.site.index_title = "Verwaltung"

urlpatterns = [
    path("", include("app_strava_webhook.api.urls")),
    path("admin/", admin.site.urls),
    path("api/", include("app_auth.api.urls")),
    path("api/", include("app_dashboard.api.urls")),
    path("api/", include("app_maintenance.api.urls")),
]
