"""
Tests fuer "Kudo", den KI-Assistenten fuers Bike-Anlegen
(`app_maintenance/api/bike_assistant.py` + die beiden Assistant-Endpoints).

Wichtigste Aussage der Suite: die KI darf **nur aus dem bestehenden Katalog waehlen**.
Erfundene oder verwechselte Template-IDs muessen serverseitig herausfallen, bevor sie
irgendwo landen koennen — der Prompt allein ist keine Absicherung.
"""

import json
from unittest.mock import patch

import requests
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from app_auth.models import StravaProfile
from app_maintenance.api.bike_assistant import suggest_models, suggest_setup
from app_maintenance.models import (
    Bike,
    BikeType,
    ComponentCategory,
    ComponentGroup,
    ComponentTemplate,
    MaintenanceKind,
)


def _gemini_json(payload) -> object:
    """Gemini-Antwortform mit JSON-Text im ersten Part."""

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            text = payload if isinstance(payload, str) else json.dumps(payload)
            return {"candidates": [{"content": {"parts": [{"text": text}]}}]}

    return _Resp()


@override_settings(AI_PROVIDER="gemini", GEMINI_API_KEY="k", GROQ_API_KEY="")
class KudoCatalogGuardTests(APITestCase):
    """Der Katalog-Filter ist die eigentliche Sicherheitsgrenze."""

    def setUp(self):
        user = get_user_model().objects.create_user(username="kudo")
        self.profile = StravaProfile.objects.create(
            user=user,
            strava_athlete_id=445566,
            access_token="t",
            refresh_token="r",
            expires_at=0,
        )
        self.bike = Bike.objects.create(
            athlete=self.profile,
            strava_bike_id="kudo-bike",
            name="Kudo Bike",
            bike_type=BikeType.GRAVEL,
        )
        self.group = ComponentGroup.objects.create(
            name="Antrieb", category=ComponentCategory.DRIVETRAIN, sort_order=1
        )
        self.other_group = ComponentGroup.objects.create(
            name="Bremsen", category=ComponentCategory.BRAKES, sort_order=2
        )
        self.chain = ComponentTemplate.objects.create(
            name="Kette",
            category=ComponentCategory.DRIVETRAIN,
            group=self.group,
            warn_km=4000,
            maintenance_kind=MaintenanceKind.PART,
        )
        self.lube = ComponentTemplate.objects.create(
            name="Kettenschmierung",
            category=ComponentCategory.DRIVETRAIN,
            group=self.group,
            warn_km=300,
            maintenance_kind=MaintenanceKind.CONSUMABLE,
        )
        self.pads = ComponentTemplate.objects.create(
            name="Bremsbelaege",
            category=ComponentCategory.BRAKES,
            group=self.other_group,
            warn_km=2000,
            maintenance_kind=MaintenanceKind.PART,
        )

    def _setup_response(self, groups):
        return _gemini_json({"groups": groups})

    @patch("app_maintenance.api.ai_providers.requests.post")
    def test_accepts_valid_catalog_reference(self, mock_post):
        mock_post.return_value = self._setup_response(
            [
                {
                    "group_id": self.group.id,
                    "parts": [
                        {
                            "template_id": self.chain.id,
                            "include": True,
                            "brand": "Shimano",
                            "model_name": "CN-M8100",
                            "custom_warn_km": 4500,
                            "confidence": "high",
                        }
                    ],
                    "intervals": [
                        {
                            "template_id": self.lube.id,
                            "include": True,
                            "interval_km": 300,
                        }
                    ],
                }
            ]
        )

        result = suggest_setup(self.bike, "Canyon", "Grail", 2022)

        self.assertEqual(len(result["groups"]), 1)
        part = result["groups"][0]["parts"][0]
        self.assertEqual(part["template_id"], self.chain.id)
        self.assertEqual(part["brand"], "Shimano")
        self.assertEqual(part["custom_warn_km"], 4500)
        self.assertEqual(
            result["groups"][0]["intervals"][0]["template_id"], self.lube.id
        )

    @patch("app_maintenance.api.ai_providers.requests.post")
    def test_hallucinated_template_id_is_dropped(self, mock_post):
        """Eine ID, die es gar nicht gibt, darf nicht durchkommen."""
        mock_post.return_value = self._setup_response(
            [
                {
                    "group_id": self.group.id,
                    "parts": [
                        {"template_id": 999999, "include": True, "brand": "Erfunden"},
                        {
                            "template_id": self.chain.id,
                            "include": True,
                            "brand": "Shimano",
                        },
                    ],
                    "intervals": [],
                }
            ]
        )

        result = suggest_setup(self.bike, "Canyon", "Grail", 2022)

        parts = result["groups"][0]["parts"]
        self.assertEqual([p["template_id"] for p in parts], [self.chain.id])

    @patch("app_maintenance.api.ai_providers.requests.post")
    def test_template_from_another_group_is_dropped(self, mock_post):
        """Bremsbelaege gehoeren nicht in die Antriebs-Baugruppe."""
        mock_post.return_value = self._setup_response(
            [
                {
                    "group_id": self.group.id,
                    "parts": [{"template_id": self.pads.id, "include": True}],
                    "intervals": [],
                }
            ]
        )

        result = suggest_setup(self.bike, "Canyon", "Grail", 2022)
        self.assertEqual(result["groups"], [])

    @patch("app_maintenance.api.ai_providers.requests.post")
    def test_consumable_offered_as_part_is_dropped(self, mock_post):
        """Kettenschmierung ist Verbrauchsmaterial, kein Verschleissteil."""
        mock_post.return_value = self._setup_response(
            [
                {
                    "group_id": self.group.id,
                    "parts": [{"template_id": self.lube.id, "include": True}],
                    "intervals": [],
                }
            ]
        )

        result = suggest_setup(self.bike, "Canyon", "Grail", 2022)
        self.assertEqual(result["groups"], [])

    @patch("app_maintenance.api.ai_providers.requests.post")
    def test_unknown_group_is_dropped(self, mock_post):
        mock_post.return_value = self._setup_response(
            [
                {
                    "group_id": 424242,
                    "parts": [{"template_id": self.chain.id}],
                    "intervals": [],
                }
            ]
        )

        result = suggest_setup(self.bike, "Canyon", "Grail", 2022)
        self.assertEqual(result["groups"], [])

    @patch("app_maintenance.api.ai_providers.requests.post")
    def test_invalid_lifetime_values_are_ignored(self, mock_post):
        """Null oder Text als Lebensdauer wuerde die Wear-Rechnung stoeren."""
        mock_post.return_value = self._setup_response(
            [
                {
                    "group_id": self.group.id,
                    "parts": [
                        {
                            "template_id": self.chain.id,
                            "custom_warn_km": 0,
                            "custom_warn_days": "viele",
                        }
                    ],
                    "intervals": [],
                }
            ]
        )

        part = suggest_setup(self.bike, "Canyon", "Grail", 2022)["groups"][0]["parts"][
            0
        ]
        self.assertIsNone(part["custom_warn_km"])
        self.assertIsNone(part["custom_warn_days"])

    @patch("app_maintenance.api.ai_providers.requests.post")
    def test_unknown_confidence_falls_back_to_medium(self, mock_post):
        mock_post.return_value = self._setup_response(
            [
                {
                    "group_id": self.group.id,
                    "parts": [
                        {"template_id": self.chain.id, "confidence": "absolut sicher"}
                    ],
                    "intervals": [],
                }
            ]
        )

        part = suggest_setup(self.bike, "Canyon", "Grail", 2022)["groups"][0]["parts"][
            0
        ]
        self.assertEqual(part["confidence"], "medium")

    @patch("app_maintenance.api.ai_providers.requests.post")
    def test_json_wrapped_in_markdown_fence_is_parsed(self, mock_post):
        """LLMs verpacken JSON gern in ```json trotz gegenteiliger Anweisung."""
        fenced = (
            "```json\n"
            + json.dumps(
                {
                    "groups": [
                        {
                            "group_id": self.group.id,
                            "parts": [
                                {"template_id": self.chain.id, "brand": "Shimano"}
                            ],
                            "intervals": [],
                        }
                    ]
                }
            )
            + "\n```"
        )
        mock_post.return_value = _gemini_json(fenced)

        result = suggest_setup(self.bike, "Canyon", "Grail", 2022)
        self.assertEqual(result["groups"][0]["parts"][0]["brand"], "Shimano")

    @patch("app_maintenance.api.ai_providers.requests.post")
    def test_broken_json_yields_none(self, mock_post):
        mock_post.return_value = _gemini_json("das ist kein JSON {{{")
        self.assertIsNone(suggest_setup(self.bike, "Canyon", "Grail", 2022))

    @patch("app_maintenance.api.ai_providers.requests.post")
    def test_provider_outage_yields_none(self, mock_post):
        mock_post.side_effect = requests.exceptions.Timeout("timed out")
        self.assertIsNone(suggest_setup(self.bike, "Canyon", "Grail", 2022))


@override_settings(AI_PROVIDER="gemini", GEMINI_API_KEY="k", GROQ_API_KEY="")
class KudoModelSuggestionTests(APITestCase):
    @patch("app_maintenance.api.ai_providers.requests.post")
    def test_returns_model_candidates(self, mock_post):
        mock_post.return_value = _gemini_json(
            {
                "models": [
                    {"model": "Grail", "year_range": "2018-2024", "note": "Gravel"},
                    {
                        "model": "Endurace",
                        "year_range": "2015-2024",
                        "note": "Endurance",
                    },
                ]
            }
        )

        models = suggest_models("Canyon", 2022, "gravel")
        self.assertEqual([m["model"] for m in models], ["Grail", "Endurace"])

    @patch("app_maintenance.api.ai_providers.requests.post")
    def test_caps_the_number_of_suggestions(self, mock_post):
        mock_post.return_value = _gemini_json(
            {"models": [{"model": f"M{i}"} for i in range(30)]}
        )
        self.assertLessEqual(len(suggest_models("Canyon", None, "gravel")), 8)

    @patch("app_maintenance.api.ai_providers.requests.post")
    def test_entries_without_model_name_are_skipped(self, mock_post):
        mock_post.return_value = _gemini_json(
            {"models": [{"note": "ohne Namen"}, {"model": "Grail"}]}
        )
        self.assertEqual(
            [m["model"] for m in suggest_models("Canyon", None, "gravel")], ["Grail"]
        )

    @patch("app_maintenance.api.ai_providers.requests.post")
    def test_unknown_manufacturer_yields_empty_list_not_none(self, mock_post):
        """Leere Liste = "kenne ich nicht"; None = "KI kaputt". Das ist ein Unterschied."""
        mock_post.return_value = _gemini_json({"models": []})
        self.assertEqual(suggest_models("Phantasiemarke", None, "gravel"), [])


@override_settings(AI_PROVIDER="gemini", GEMINI_API_KEY="k", GROQ_API_KEY="")
class AssistantEndpointTests(KudoCatalogGuardTests):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.profile.user)
        session = self.client.session
        session["strava_athlete_id"] = self.profile.strava_athlete_id
        session.save()

    @patch("app_maintenance.api.ai_providers.requests.post")
    def test_models_endpoint_returns_candidates(self, mock_post):
        mock_post.return_value = _gemini_json({"models": [{"model": "Grail"}]})

        response = self.client.post(
            "/api/maintenance/assistant/models/",
            {"manufacturer": "Canyon", "year": 2022, "bike_type": "gravel"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["models"][0]["model"], "Grail")

    @patch("app_maintenance.api.ai_providers.requests.post")
    def test_setup_endpoint_returns_prefill(self, mock_post):
        mock_post.return_value = self._setup_response(
            [
                {
                    "group_id": self.group.id,
                    "parts": [{"template_id": self.chain.id, "brand": "Shimano"}],
                    "intervals": [],
                }
            ]
        )

        response = self.client.post(
            f"/api/maintenance/bikes/{self.bike.id}/assistant/setup/",
            {"manufacturer": "Canyon", "model": "Grail", "year": 2022},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["groups"][0]["group_name"], "Antrieb")

    @patch("app_maintenance.api.ai_providers.requests.post")
    def test_ai_outage_returns_503_so_manual_setup_stays_possible(self, mock_post):
        mock_post.side_effect = requests.exceptions.Timeout("timed out")

        response = self.client.post(
            "/api/maintenance/assistant/models/",
            {"manufacturer": "Canyon"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(response.data["code"], "ai_unavailable")

    def test_missing_manufacturer_is_rejected(self):
        response = self.client.post(
            "/api/maintenance/assistant/models/", {}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_setup_for_foreign_bike_is_404(self):
        other_user = get_user_model().objects.create_user(username="fremd")
        other_profile = StravaProfile.objects.create(
            user=other_user,
            strava_athlete_id=998877,
            access_token="t",
            refresh_token="r",
            expires_at=0,
        )
        foreign_bike = Bike.objects.create(
            athlete=other_profile,
            strava_bike_id="fremd-bike",
            name="Fremd",
            bike_type=BikeType.ROAD,
        )

        response = self.client.post(
            f"/api/maintenance/bikes/{foreign_bike.id}/assistant/setup/",
            {"manufacturer": "Canyon", "model": "Grail"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_requires_authentication(self):
        self.client.logout()
        response = self.client.post(
            "/api/maintenance/assistant/models/",
            {"manufacturer": "Canyon"},
            format="json",
        )
        self.assertIn(
            response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )


@override_settings(AI_PROVIDER="gemini", GEMINI_API_KEY="k", GROQ_API_KEY="")
class KudoPromptTests(APITestCase):
    """
    Tests fuer den Prompt selbst, nicht fuer den Filter dahinter.

    Anlass war ein realer Fehler: das Few-Shot-Beispiel im System-Prompt benutzte
    echte Katalog-IDs, die etwas voellig anderes bedeuteten als das, wofuer sie im
    Beispiel standen (group_id 1 = "Laufrad vorne", darin template_id 2 = "Kassette"
    aus Gruppe 2, mit einer Ketten-Modellnummer als Marke; template_id 9 = "Ritzel",
    ein Verschleissteil, stand unter "intervals"). Das Beispiel fuehrte dem Modell
    also genau die drei Fehler vor, die `_clean_row()` hinterher wegwirft.
    """

    def setUp(self):
        user = get_user_model().objects.create_user(username="kudoprompt")
        self.profile = StravaProfile.objects.create(
            user=user,
            strava_athlete_id=778899,
            access_token="t",
            refresh_token="r",
            expires_at=0,
        )
        self.bike = Bike.objects.create(
            athlete=self.profile,
            strava_bike_id="kudo-prompt-bike",
            name="Prompt Bike",
            bike_type=BikeType.GRAVEL,
        )
        self.group = ComponentGroup.objects.create(
            name="Antrieb", category=ComponentCategory.DRIVETRAIN, sort_order=1
        )
        self.common = ComponentTemplate.objects.create(
            name="Kette",
            category=ComponentCategory.DRIVETRAIN,
            group=self.group,
            warn_km=4000,
            maintenance_kind=MaintenanceKind.PART,
            default_in_group=True,
        )
        self.rare = ComponentTemplate.objects.create(
            name="Zahnriemen",
            category=ComponentCategory.DRIVETRAIN,
            group=self.group,
            warn_days=1095,
            maintenance_kind=MaintenanceKind.PART,
            default_in_group=False,
        )

    def _captured_system_prompt(self, mock_post) -> str:
        """Der System-Prompt, wie er tatsaechlich an den Provider ging."""
        body = mock_post.call_args.kwargs["json"]
        return json.dumps(body, ensure_ascii=False)

    @patch("app_maintenance.api.ai_providers.requests.post")
    def test_example_ids_in_the_prompt_are_not_real_catalog_ids(self, mock_post):
        """
        Regressionstest fuer den Anlass dieser Klasse: die Platzhalter-IDs im
        Format-Beispiel duerfen mit keiner echten Gruppen- oder Template-ID
        kollidieren, sonst widerspricht das Beispiel dem mitgelieferten Katalog.
        """
        mock_post.return_value = _gemini_json({"groups": []})
        suggest_setup(self.bike, "Canyon", "Grail", 2022)

        prompt = self._captured_system_prompt(mock_post)
        real_ids = set(ComponentTemplate.objects.values_list("id", flat=True)) | set(
            ComponentGroup.objects.values_list("id", flat=True)
        )
        for placeholder in (9001, 9002, 9003):
            self.assertIn(str(placeholder), prompt)
            self.assertNotIn(
                placeholder,
                real_ids,
                f"Platzhalter {placeholder} kollidiert mit einer echten Katalog-ID.",
            )

    @patch("app_maintenance.api.ai_providers.requests.post")
    def test_catalog_marks_usual_versus_special_templates(self, mock_post):
        """
        `default_in_group` muss im Prompt stehen: das Frontend laesst `hint.include`
        der KI diesen Default ueberschreiben, also darf die KI ihn nicht raten.
        """
        mock_post.return_value = _gemini_json({"groups": []})
        suggest_setup(self.bike, "Canyon", "Grail", 2022)

        prompt = self._captured_system_prompt(mock_post)
        self.assertIn(f"Template {self.common.id}: Kette", prompt)
        self.assertIn("ueblich", prompt)
        self.assertIn(f"Template {self.rare.id}: Zahnriemen", prompt)
        self.assertIn("Sonderfall", prompt)

    @patch("app_maintenance.api.ai_providers.requests.post")
    def test_missing_lifetime_axis_is_omitted_not_shown_as_question_mark(
        self, mock_post
    ):
        """
        Frueher stand "4000.0 km / ? Tage" im Katalog — das "?" las sich wie ein
        auszufuellendes Feld, und die Nachkommastelle war bedeutungslos.
        """
        mock_post.return_value = _gemini_json({"groups": []})
        suggest_setup(self.bike, "Canyon", "Grail", 2022)

        prompt = self._captured_system_prompt(mock_post)
        self.assertIn("4000 km", prompt)
        self.assertNotIn("4000.0", prompt)
        self.assertNotIn("? Tage", prompt)
        self.assertNotIn("? km", prompt)
