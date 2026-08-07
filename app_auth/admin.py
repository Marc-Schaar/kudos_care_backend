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
    ]
    list_filter = ["email_notifications_enabled", "sync_status"]
    search_fields = ["strava_athlete_id", "user__username", "user__email"]
    actions = ["send_welcome_email"]

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
