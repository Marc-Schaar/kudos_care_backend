import itertools
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.core import mail
from django.utils import timezone
from rest_framework.test import APITestCase

from app_auth.models import StravaProfile
from app_dashboard.api.services import predict_next_ride_date
from app_dashboard.models import Ride
from app_maintenance.models import (
    Bike,
    BikeType,
    Component,
    ComponentCategory,
    ComponentSlot,
    ComponentTemplate,
)
from app_notifications.services import send_templated_email
from app_notifications.tasks import (
    check_bike_unsafe_predictions,
    check_component_warnings,
    check_component_warnings_for_bike,
    send_welcome_email_task,
)


def _make_profile(user, strava_athlete_id=80001, email_enabled=True):
    return StravaProfile.objects.create(
        user=user,
        strava_athlete_id=strava_athlete_id,
        access_token="token",
        refresh_token="refresh",
        expires_at=0,
        email_notifications_enabled=email_enabled,
    )


class SendTemplatedEmailTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="mail-athlete", password="pw", email="athlete@example.com"
        )
        self.profile = _make_profile(self.user)

    def test_sends_email_when_enabled_and_address_present(self):
        sent = send_templated_email(
            self.profile,
            subject="Test-Betreff",
            template_name="emails/welcome.html",
            context={},
        )
        self.assertTrue(sent)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["athlete@example.com"])
        self.assertEqual(mail.outbox[0].subject, "Test-Betreff")
        self.assertEqual(mail.outbox[0].alternatives[0][1], "text/html")

    def test_skips_when_notifications_disabled(self):
        self.profile.email_notifications_enabled = False
        self.profile.save(update_fields=["email_notifications_enabled"])

        sent = send_templated_email(
            self.profile,
            subject="Test",
            template_name="emails/welcome.html",
            context={},
        )

        self.assertFalse(sent)
        self.assertEqual(len(mail.outbox), 0)

    def test_skips_when_no_email_address(self):
        self.user.email = ""
        self.user.save(update_fields=["email"])

        sent = send_templated_email(
            self.profile,
            subject="Test",
            template_name="emails/welcome.html",
            context={},
        )

        self.assertFalse(sent)
        self.assertEqual(len(mail.outbox), 0)


class PredictNextRideDateTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="predict-athlete", password="pw"
        )
        self.profile = _make_profile(self.user, strava_athlete_id=80002)
        self.bike = Bike.objects.create(
            athlete=self.profile,
            strava_bike_id="pb1",
            name="Vorhersagerad",
            bike_type=BikeType.ROAD,
        )
        self._ride_id_counter = itertools.count(90000)

    def _create_rides(self, gap_days, count, last_ride_days_ago):
        now = timezone.now()
        for i in range(count):
            Ride.objects.create(
                strava_id=next(self._ride_id_counter),
                name=f"Fahrt {i}",
                distance=20000,
                start_date=now - timedelta(days=last_ride_days_ago + i * gap_days),
                athlete=self.profile,
                bike=self.bike,
            )

    def test_returns_none_with_insufficient_history(self):
        self._create_rides(gap_days=7, count=2, last_ride_days_ago=6)
        self.assertIsNone(predict_next_ride_date(self.bike))

    def test_predicts_median_gap_from_last_ride(self):
        self._create_rides(gap_days=7, count=5, last_ride_days_ago=6)
        predicted = predict_next_ride_date(self.bike)
        self.assertEqual(predicted, date.today() + timedelta(days=1))

    def test_clamps_to_today_when_overdue(self):
        self._create_rides(gap_days=7, count=5, last_ride_days_ago=30)
        predicted = predict_next_ride_date(self.bike)
        self.assertEqual(predicted, date.today())


class ComponentWarningDigestTestsBase(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="warn-athlete", password="pw", email="warn@example.com"
        )
        self.profile = _make_profile(self.user, strava_athlete_id=80003)

    def _make_bike(self, strava_bike_id):
        return Bike.objects.create(
            athlete=self.profile,
            strava_bike_id=strava_bike_id,
            name=f"Bike {strava_bike_id}",
            bike_type=BikeType.ROAD,
        )

    def _make_overdue_component(
        self, bike, warn_days=10, installed_days_ago=100, name="Kette"
    ):
        template = ComponentTemplate.objects.create(
            name=name,
            category=ComponentCategory.DRIVETRAIN,
            warn_days=warn_days,
            is_system=False,
        )
        slot = ComponentSlot.objects.create(bike=bike, template=template)
        component = Component.objects.create(
            slot=slot,
            brand="Test",
            installed_at=date.today() - timedelta(days=installed_days_ago),
            is_mounted=True,
        )
        return slot, component


class CheckComponentWarningsTests(ComponentWarningDigestTestsBase):
    def test_sends_digest_for_new_warning_and_sets_dedupe(self):
        bike = self._make_bike("cw1")
        _, component = self._make_overdue_component(bike)

        check_component_warnings()

        self.assertEqual(len(mail.outbox), 1)
        component.refresh_from_db()
        self.assertEqual(component.last_warn_notified_status, "critical")

    def test_second_run_does_not_resend_for_unchanged_status(self):
        bike = self._make_bike("cw2")
        self._make_overdue_component(bike)

        check_component_warnings()
        self.assertEqual(len(mail.outbox), 1)

        check_component_warnings()
        self.assertEqual(len(mail.outbox), 1)

    def test_escalation_resends_even_if_previously_notified(self):
        bike = self._make_bike("cw3")
        _, component = self._make_overdue_component(
            bike, warn_days=10, installed_days_ago=100
        )
        component.last_warn_notified_status = "warn"
        component.save(update_fields=["last_warn_notified_status"])

        check_component_warnings()

        self.assertEqual(len(mail.outbox), 1)

    def test_bundles_multiple_components_into_one_email(self):
        bike1 = self._make_bike("cw4a")
        bike2 = self._make_bike("cw4b")
        self._make_overdue_component(bike1, name="Kette")
        self._make_overdue_component(bike2, name="Bremsbeläge")

        check_component_warnings()

        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].alternatives[0][0]
        self.assertIn("Kette", body)
        self.assertIn("Bremsbeläge", body)

    def test_skips_profile_without_email(self):
        self.user.email = ""
        self.user.save(update_fields=["email"])
        bike = self._make_bike("cw5")
        self._make_overdue_component(bike)

        check_component_warnings()

        self.assertEqual(len(mail.outbox), 0)


class CheckComponentWarningsForBikeTests(ComponentWarningDigestTestsBase):
    def test_sends_immediate_email_and_shares_dedupe_with_daily_task(self):
        bike = self._make_bike("cwb1")
        _, component = self._make_overdue_component(bike)

        check_component_warnings_for_bike(bike.id)

        self.assertEqual(len(mail.outbox), 1)
        component.refresh_from_db()
        self.assertEqual(component.last_warn_notified_status, "critical")

        # Der taegliche Voll-Scan darf denselben, bereits gemeldeten Status nicht
        # erneut versenden — gemeinsamer Dedupe via last_warn_notified_status.
        check_component_warnings()
        self.assertEqual(len(mail.outbox), 1)

    def test_only_reports_the_given_bike(self):
        bike1 = self._make_bike("cwb2a")
        bike2 = self._make_bike("cwb2b")
        self._make_overdue_component(bike1, name="Kette")
        _, comp2 = self._make_overdue_component(bike2, name="Bremsbeläge")

        check_component_warnings_for_bike(bike1.id)

        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].alternatives[0][0]
        self.assertIn("Kette", body)
        self.assertNotIn("Bremsbeläge", body)

        comp2.refresh_from_db()
        self.assertEqual(comp2.last_warn_notified_status, "")


class CheckBikeUnsafePredictionsTests(ComponentWarningDigestTestsBase):
    def setUp(self):
        super().setUp()
        self._ride_id_counter = itertools.count(91000)

    def _add_ride_history(self, bike, gap_days=7, count=5, last_ride_days_ago=6):
        now = timezone.now()
        for i in range(count):
            Ride.objects.create(
                strava_id=next(self._ride_id_counter),
                name="Fahrt",
                distance=20000,
                start_date=now - timedelta(days=last_ride_days_ago + i * gap_days),
                athlete=self.profile,
                bike=bike,
            )

    def test_sends_when_projected_critical_but_not_today(self):
        bike = self._make_bike("up1")
        self._add_ride_history(bike)  # predicted date = heute + 1
        # warn_days=6, installiert vor 5 Tagen -> heute ratio 5/6 (warn), am
        # vorhergesagten Tag (heute+1) ratio 6/6=1.0 (critical).
        self._make_overdue_component(bike, warn_days=6, installed_days_ago=5)

        check_bike_unsafe_predictions()

        self.assertEqual(len(mail.outbox), 1)
        bike.refresh_from_db()
        self.assertEqual(
            bike.predicted_unsafe_notified_for_date, date.today() + timedelta(days=1)
        )

    def test_skips_when_already_critical_today(self):
        bike = self._make_bike("up2")
        self._add_ride_history(bike)
        self._make_overdue_component(bike, warn_days=6, installed_days_ago=100)

        check_bike_unsafe_predictions()

        self.assertEqual(len(mail.outbox), 0)

    def test_does_not_resend_for_same_predicted_date(self):
        bike = self._make_bike("up3")
        self._add_ride_history(bike)
        self._make_overdue_component(bike, warn_days=6, installed_days_ago=5)

        check_bike_unsafe_predictions()
        self.assertEqual(len(mail.outbox), 1)

        check_bike_unsafe_predictions()
        self.assertEqual(len(mail.outbox), 1)

    def test_skips_bikes_without_enough_ride_history(self):
        bike = self._make_bike("up4")
        self._make_overdue_component(bike, warn_days=6, installed_days_ago=5)

        check_bike_unsafe_predictions()

        self.assertEqual(len(mail.outbox), 0)


class SendWelcomeEmailTaskTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="welcome-athlete", password="pw", email="welcome@example.com"
        )
        self.profile = _make_profile(self.user, strava_athlete_id=80004)

    def test_sends_email_and_sets_timestamp(self):
        self.assertIsNone(self.profile.welcome_email_sent_at)

        send_welcome_email_task(self.profile.id)

        self.assertEqual(len(mail.outbox), 1)
        self.profile.refresh_from_db()
        self.assertIsNotNone(self.profile.welcome_email_sent_at)

    def test_missing_profile_does_not_raise(self):
        result = send_welcome_email_task(999999)
        self.assertIn("nicht gefunden", result)
        self.assertEqual(len(mail.outbox), 0)
