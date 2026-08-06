from django.contrib import admin

from .models import StravaProfile


@admin.register(StravaProfile)
class StravaProfileAdmin(admin.ModelAdmin):
    list_display = [
        "__str__",
        "user",
        "strava_athlete_id",
        "sync_status",
        "sync_finished_at",
        "last_sync_count",
    ]
    list_filter = ["sync_status"]
    search_fields = ["firstname", "lastname", "strava_athlete_id", "user__username", "user__email"]
    autocomplete_fields = ["user"]

    fieldsets = [
        (
            None,
            {"fields": ["user", "strava_athlete_id", "firstname", "lastname"]},
        ),
        (
            "Tokens",
            {
                "fields": ["access_token_preview", "refresh_token_preview", "expires_at"],
                "description": "Volle Token-Werte werden aus Sicherheitsgründen nicht angezeigt.",
            },
        ),
        (
            "Sync-Status",
            {
                "fields": [
                    "sync_status",
                    "sync_started_at",
                    "sync_finished_at",
                    "sync_error",
                    "last_sync_count",
                    "sync_task_id",
                    "sync_progress_current",
                    "sync_progress_total",
                ]
            },
        ),
    ]
    readonly_fields = [
        "access_token_preview",
        "refresh_token_preview",
        "sync_started_at",
        "sync_finished_at",
        "sync_task_id",
        "sync_progress_current",
        "sync_progress_total",
    ]

    @admin.display(description="Access Token")
    def access_token_preview(self, obj):
        return self._mask(obj.access_token)

    @admin.display(description="Refresh Token")
    def refresh_token_preview(self, obj):
        return self._mask(obj.refresh_token)

    @staticmethod
    def _mask(value: str) -> str:
        if not value:
            return "—"
        return f"…{value[-6:]}"
