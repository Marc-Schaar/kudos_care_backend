"""
Was die Mails anzeigen, ohne DB: die Zeile je Teil, Gruppierung, Betreff und das
gerenderte Template. Komponenten als Stubs, wie in `app_maintenance/test_usage_periods.py`.
"""

from datetime import date
from types import SimpleNamespace

from django.template.loader import render_to_string
from django.test import SimpleTestCase

from app_notifications.presentation import (
    format_day,
    group_predictions,
    group_warnings,
    prediction_subject,
    warning_headline,
    warning_subject,
    wear_line,
)
from app_notifications.services import html_to_plaintext


def _part(label="", warn_km=None, warn_days=None, weather_km=None):
    return SimpleNamespace(
        label=label,
        effective_warn_km=warn_km,
        effective_warn_days=warn_days,
        weather_wear_km=weather_km,
    )


def _wear(km=0.0, days=None, status="critical"):
    return {"wear_km": km, "wear_days": days, "warn_status_overall": status}


def _warning(bike, name, component, wear):
    return {
        "bike": bike,
        "slot": SimpleNamespace(display_name=name),
        "component": component,
        "wear": wear,
    }


class WearLineTests(SimpleTestCase):
    """Dieselbe Regel wie `partRemaining()` im Frontend — App und Mail nennen dieselbe Zahl."""

    def test_over_the_km_limit_counts_on(self):
        line = wear_line("Kette", _part(warn_km=3000), _wear(km=3312.4))
        self.assertEqual(line.remaining, "312 km drüber")
        self.assertEqual(line.detail, "3.312 von 3.000 km")
        self.assertEqual(line.bar_pct, 100)
        self.assertEqual(line.rest_pct, 0)

    def test_below_the_limit_says_what_is_left(self):
        line = wear_line(
            "Bremsbeläge", _part(warn_km=2000), _wear(km=1712, status="warn")
        )
        self.assertEqual(line.remaining, "noch 288 km")
        self.assertEqual(line.bar_pct, 86)
        self.assertEqual(line.shape, "▲")

    def test_weather_surcharge_drives_the_km_axis(self):
        line = wear_line("Kette", _part(warn_km=3000, weather_km=3312), _wear(km=2980))
        self.assertEqual(line.remaining, "312 km drüber")
        self.assertIn("mit Wetteraufschlag", line.detail)

    def test_days_axis_wins_when_further_along(self):
        line = wear_line(
            "Dichtmilch", _part(warn_km=10000, warn_days=120), _wear(km=1000, days=143)
        )
        self.assertEqual(line.remaining, "23 Tage drüber")
        self.assertEqual(line.detail, "143 von 120 Tagen")

    def test_singular_day(self):
        line = wear_line(
            "Dichtmilch", _part(warn_days=120), _wear(days=119, status="warn")
        )
        self.assertEqual(line.remaining, "noch 1 Tag")

    def test_without_limits_there_is_no_bar_and_no_number(self):
        line = wear_line("Sattel", _part(), _wear(km=500, status="unknown"))
        self.assertEqual(line.remaining, "")
        self.assertIsNone(line.bar_pct)

    def test_a_fresh_part_still_shows_a_sliver(self):
        line = wear_line("Kette", _part(warn_km=3000), _wear(km=1, status="ok"))
        self.assertEqual(line.bar_pct, 3)


class GroupingAndSubjectTests(SimpleTestCase):
    def setUp(self):
        self.gravel = SimpleNamespace(id=1, name="Gravel")
        self.road = SimpleNamespace(id=2, name="Renner")

    def test_groups_by_bike_with_the_most_urgent_first(self):
        groups = group_warnings(
            [
                _warning(
                    self.gravel,
                    "Bremsbeläge",
                    _part(warn_km=2000),
                    _wear(1700, status="warn"),
                ),
                _warning(self.road, "Reifen", _part(warn_km=4000), _wear(4100)),
                _warning(self.gravel, "Kette", _part(warn_km=3000), _wear(3300)),
            ]
        )
        self.assertEqual([g.bike.name for g in groups], ["Gravel", "Renner"])
        self.assertEqual(
            [line.name for line in groups[0].lines], ["Kette", "Bremsbeläge"]
        )

    def test_subject_names_the_part_when_there_is_one(self):
        groups = group_warnings(
            [_warning(self.gravel, "Kette", _part(warn_km=3000), _wear(3312))]
        )
        self.assertEqual(warning_subject(groups), "Kette: 312 km drüber")
        self.assertEqual(warning_headline(groups), "1 Teil überfällig")

    def test_subject_counts_the_rest(self):
        groups = group_warnings(
            [
                _warning(self.gravel, "Kette", _part(warn_km=3000), _wear(3312)),
                _warning(
                    self.gravel,
                    "Bremsbeläge",
                    _part(warn_km=2000),
                    _wear(1700, status="warn"),
                ),
            ]
        )
        self.assertEqual(warning_subject(groups), "Kette und 1 weiteres Teil fällig")
        self.assertEqual(warning_headline(groups), "1 Teil überfällig, 1 bald fällig")

    def test_prediction_subject_and_german_date(self):
        groups = group_predictions(
            [
                {
                    "bike": self.gravel,
                    "predicted_date": date(2026, 11, 21),
                    "components": [
                        {
                            "slot": SimpleNamespace(display_name="Reifen hinten"),
                            "component": _part(warn_days=730),
                            "wear": _wear(days=734),
                        }
                    ],
                }
            ]
        )
        self.assertEqual(
            prediction_subject(groups), "Vor der nächsten Fahrt: Reifen hinten prüfen"
        )
        self.assertEqual(groups[0].predicted_day, "Samstag, 21. November")
        self.assertEqual(format_day(date(2026, 3, 2)), "Montag, 2. März")


class RenderedWarningMailTests(SimpleTestCase):
    def _render(self):
        bike = SimpleNamespace(id=7, name="Gravel")
        groups = group_warnings(
            [
                _warning(
                    bike, "Kette", _part("Shimano CN-M8100", warn_km=3000), _wear(3312)
                )
            ]
        )
        return render_to_string(
            "emails/component_warnings.html",
            {
                "frontend_url": "https://kudos.example.com",
                "groups": groups,
                "headline": warning_headline(groups),
                "immediate": True,
            },
        )

    def test_shows_the_number_to_act_on_and_links_to_the_bike(self):
        html = self._render()
        self.assertIn("312 km drüber", html)
        self.assertIn("Shimano CN-M8100", html)
        self.assertIn('href="https://kudos.example.com/app/maintenance/bikes/7"', html)

    def test_no_raw_status_slug_in_the_visible_text(self):
        text = html_to_plaintext(self._render())
        self.assertNotIn("critical", text)
        self.assertNotIn("? km", text)
        self.assertIn("◆", text)
        self.assertIn("Kette", text)
