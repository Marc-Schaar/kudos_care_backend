from django.contrib import admin

from .models import StravaProfile


@admin.register(StravaProfile)
class StravaProfileAdmin(admin.ModelAdmin):
    list_display = [
        "strava_athlete_id",
        "user",
        "email_notifications_enabled",
        "welcome_email_sent_at",
        "sync_status",
        "sync_finished_at",
        "last_sync_count",
    ]
    list_filter = ["email_notifications_enabled", "sync_status"]
    search_fields = ["strava_athlete_id", "user__username", "user__email"]
    autocomplete_fields = ["user"]
    actions = ["send_welcome_email"]

    fieldsets = [
        (
            None,
            {"fields": ["user", "strava_athlete_id"]},
        ),
        (
            "Benachrichtigungen",
            {"fields": ["email_notifications_enabled", "welcome_email_sent_at"]},
        ),
        (
            "Tokens",
            {
                "fields": [
                    "access_token_preview",
                    "refresh_token_preview",
                    "expires_at",
                ],
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
        "welcome_email_sent_at",
        "access_token_preview",
        "refresh_token_preview",
        "sync_started_at",
        "sync_finished_at",
        "sync_task_id",
        "sync_progress_current",
        "sync_progress_total",
    ]

    @admin.action(description="Willkommens-E-Mail senden")
    def send_welcome_email(self, request, queryset):
        from app_notifications.tasks import send_welcome_email_task

        eligible = queryset.filter(user__isnull=False).exclude(user__email="")
        skipped = queryset.count() - eligible.count()

        for profile in eligible:
            send_welcome_email_task.delay(profile.id)

        message = f"Willkommens-E-Mail für {eligible.count()} Profil(e) eingeplant."
        if skipped:
            message += (
                f" {skipped} Profil(e) übersprungen (keine E-Mail-Adresse hinterlegt)."
            )
        self.message_user(request, message)

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
