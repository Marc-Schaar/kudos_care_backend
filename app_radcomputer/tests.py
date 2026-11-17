import time
from unittest.mock import Mock, patch

import requests
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework.test import APITestCase

from app_auth.models import StravaProfile
from app_radcomputer.models import RadcomputerSettings, SavedPlace


def login_as(client, user, athlete_id: int) -> None:
    client.force_login(user)
    session = client.session
    session["strava_athlete_id"] = athlete_id
    session.save()


def fake(status=200, json=None, content=b""):
    res = Mock()
    res.status_code = status
    res.json.return_value = json if json is not None else {}
    res.content = content
    return res


class RadcomputerTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create(username="strava_4711")
        self.profile = StravaProfile.objects.create(
            user=self.user,
            strava_athlete_id=4711,
            access_token="acc-1",
            refresh_token="ref-1",
            expires_at=int(time.time()) + 3600,
            scope="activity:read_all,activity:write,profile:read_all,read_all",
        )
        self.client.force_login(self.user)
        session = self.client.session
        session["strava_athlete_id"] = 4711
        session.save()

    def upload(self):
        file = SimpleUploadedFile(
            "fahrt.tcx", b"<xml/>", content_type="application/xml"
        )
        return self.client.post(
            "/api/radcomputer/upload/",
            {"file": file, "data_type": "tcx", "name": "Feierabend", "commute": "1"},
            format="multipart",
        )

    def test_requires_login(self):
        self.client.logout()
        self.assertIn(
            self.client.get("/api/radcomputer/routes/").status_code, (401, 403)
        )

    @patch("app_auth.api.utils.requests.request")
    def test_upload_forwards_file(self, request):
        request.return_value = fake(
            201, {"id": 99, "status": "Your activity is still being processed."}
        )
        res = self.upload()
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data["id"], 99)
        method, url = request.call_args.args
        self.assertEqual(
            (method, url), ("POST", "https://www.strava.com/api/v3/uploads")
        )
        self.assertEqual(request.call_args.kwargs["files"]["file"][1], b"<xml/>")
        self.assertEqual(request.call_args.kwargs["data"]["commute"], "1")
        self.assertEqual(
            request.call_args.kwargs["headers"]["Authorization"], "Bearer acc-1"
        )

    def test_upload_without_write_scope_asks_for_permission(self):
        self.profile.scope = "activity:read_all,profile:read_all"
        self.profile.save()
        res = self.upload()
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "scope_missing")

    def test_upload_requires_file(self):
        res = self.client.post(
            "/api/radcomputer/upload/", {"data_type": "tcx"}, format="multipart"
        )
        self.assertEqual(res.status_code, 400)

    @patch("app_auth.api.utils.requests.post")
    @patch("app_auth.api.utils.requests.request")
    def test_401_triggers_one_forced_refresh(self, request, post):
        post.return_value = fake(
            json={
                "access_token": "acc-2",
                "refresh_token": "ref-2",
                "expires_at": int(time.time()) + 3600,
            }
        )
        request.side_effect = [
            fake(401),
            fake(json={"id": 1, "status": "ready", "activity_id": 5}),
        ]
        res = self.client.get("/api/radcomputer/upload/1/")
        self.assertEqual(res.data["activity_id"], 5)
        self.assertEqual(
            request.call_args.kwargs["headers"]["Authorization"], "Bearer acc-2"
        )

    @patch("app_auth.api.utils.requests.post")
    def test_revoked_access_asks_to_log_in_again(self, post):
        post.return_value = Mock(
            raise_for_status=Mock(side_effect=__import__("requests").HTTPError("400"))
        )
        self.profile.expires_at = 0
        self.profile.save()
        self.assertEqual(self.client.get("/api/radcomputer/routes/").status_code, 401)

    @patch("app_auth.api.utils.requests.request")
    def test_routes_rides_first_with_string_ids(self, request):
        request.return_value = fake(
            json=[
                {"id": 1, "id_str": "3344556677889900112", "name": "Lauf", "type": 2},
                {
                    "id": 2,
                    "id_str": "3344556677889900113",
                    "name": "Runde",
                    "type": 1,
                    "distance": 42000,
                },
            ]
        )
        res = self.client.get("/api/radcomputer/routes/")
        self.assertEqual([r["name"] for r in res.data["routes"]], ["Runde", "Lauf"])
        self.assertEqual(res.data["routes"][0]["id"], "3344556677889900113")
        self.assertIn("/athletes/4711/routes", request.call_args.args[1])

    @patch("app_auth.api.utils.requests.request", return_value=fake(content=b"<gpx/>"))
    def test_route_gpx(self, request):
        res = self.client.get("/api/radcomputer/routes/3344556677889900113/gpx/")
        self.assertEqual(res.content, b"<gpx/>")
        self.assertTrue(
            request.call_args.args[1].endswith("/routes/3344556677889900113/export_gpx")
        )

    def test_route_gpx_rejects_non_numeric_id(self):
        self.assertEqual(
            self.client.get("/api/radcomputer/routes/abc/gpx/").status_code, 400
        )

    def test_me_reports_radcomputer_permissions(self):
        res = self.client.get("/api/strava/me/")
        self.assertTrue(res.data["can_upload"])
        self.assertTrue(res.data["private_routes"])


class RadcomputerSettingsTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create(username="strava_4711")
        self.profile = StravaProfile.objects.create(
            user=self.user,
            strava_athlete_id=4711,
            access_token="a",
            refresh_token="r",
            expires_at=0,
        )
        login_as(self.client, self.user, 4711)

    def test_get_without_stored_settings_returns_defaults(self):
        res = self.client.get("/api/radcomputer/settings/")
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.data["stored"])
        self.assertEqual(res.data["ftp"], 200)
        self.assertEqual(res.data["theme"], "night")
        self.assertFalse(RadcomputerSettings.objects.exists())

    def test_put_creates_then_partial_update_keeps_other_fields(self):
        res = self.client.put(
            "/api/radcomputer/settings/",
            {"ftp": 250, "theme": "day"},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data["stored"])
        res = self.client.put(
            "/api/radcomputer/settings/", {"audio": "beep"}, format="json"
        )
        stored = RadcomputerSettings.objects.get(profile=self.profile)
        self.assertEqual((stored.ftp, stored.theme, stored.audio), (250, "day", "beep"))
        self.assertTrue(self.client.get("/api/radcomputer/settings/").data["stored"])

    def test_rejects_out_of_range_and_unknown_choice(self):
        for body in ({"ftp": 5000}, {"auto_pause_kmh": 0}, {"theme": "pink"}):
            res = self.client.put("/api/radcomputer/settings/", body, format="json")
            self.assertEqual(res.status_code, 400, body)

    def test_requires_login(self):
        self.client.logout()
        self.assertIn(
            self.client.get("/api/radcomputer/settings/").status_code, (401, 403)
        )


class SavedPlaceTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create(username="strava_4711")
        self.profile = StravaProfile.objects.create(
            user=self.user,
            strava_athlete_id=4711,
            access_token="a",
            refresh_token="r",
            expires_at=0,
        )
        login_as(self.client, self.user, 4711)

    def add(self, name="Zuhause", lat=52.5163, lon=13.3777):
        return self.client.post(
            "/api/radcomputer/places/",
            {"name": name, "lat": lat, "lon": lon},
            format="json",
        )

    def test_place_is_stored_encrypted_and_listed_in_plaintext(self):
        res = self.add()
        self.assertEqual(res.status_code, 201)
        raw = SavedPlace.objects.get().ciphertext
        # Weder Name noch Koordinaten dürfen im Klartext in der Datenbank stehen.
        for plain in ("Zuhause", "52.5163", "13.3777"):
            self.assertNotIn(plain, raw)
        listed = self.client.get("/api/radcomputer/places/").data
        self.assertEqual(
            listed,
            [{"id": res.data["id"], "name": "Zuhause", "lat": 52.5163, "lon": 13.3777}],
        )

    def test_same_name_replaces_existing_place(self):
        first = self.add(lat=50, lon=8)
        second = self.add(lat=51, lon=9)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.data["id"], second.data["id"])
        self.assertEqual(SavedPlace.objects.count(), 1)
        self.assertEqual(SavedPlace.objects.get().data["lat"], 51)

    def test_delete_and_athlete_scoping(self):
        other_user = User.objects.create(username="strava_9")
        other = StravaProfile.objects.create(
            user=other_user,
            strava_athlete_id=9,
            access_token="a",
            refresh_token="r",
            expires_at=0,
        )
        foreign = SavedPlace(profile=other)
        foreign.data = {"name": "Fremd", "lat": 1, "lon": 2}
        foreign.save()

        self.assertEqual(self.client.get("/api/radcomputer/places/").data, [])
        res = self.client.delete(f"/api/radcomputer/places/{foreign.id}/")
        self.assertEqual(res.status_code, 404)
        self.assertTrue(SavedPlace.objects.filter(id=foreign.id).exists())

        own = self.add().data["id"]
        self.assertEqual(
            self.client.delete(f"/api/radcomputer/places/{own}/").status_code, 204
        )
        self.assertFalse(SavedPlace.objects.filter(id=own).exists())

    def test_rejects_invalid_coordinates(self):
        self.assertEqual(self.add(lat=91).status_code, 400)
        self.assertEqual(self.add(name="").status_code, 400)

    def test_undecryptable_entry_is_skipped_not_fatal(self):
        self.add()
        broken = SavedPlace.objects.create(profile=self.profile, ciphertext="kaputt")
        with self.assertLogs("my_app_debug", level="ERROR") as logs:
            listed = self.client.get("/api/radcomputer/places/").data
        self.assertEqual([p["name"] for p in listed], ["Zuhause"])
        self.assertIn(str(broken.id), logs.output[0])

    def test_account_deletion_removes_places_and_settings(self):
        self.add()
        self.client.put("/api/radcomputer/settings/", {"ftp": 300}, format="json")
        res = self.client.delete("/api/strava/me/?confirm=true")
        self.assertIn(res.status_code, (200, 204))
        self.assertFalse(SavedPlace.objects.exists())
        self.assertFalse(RadcomputerSettings.objects.exists())


WIND_URL = "/api/radcomputer/wind/"


def open_meteo(current: dict | None = None, status: int = 200) -> Mock:
    res = Mock()
    res.status_code = status
    res.json.return_value = {
        "current": (
            current
            if current is not None
            else {
                "time": "2026-11-15T10:45",
                "wind_speed_10m": 18.4,
                "wind_direction_10m": 250,
                "wind_gusts_10m": 33.1,
                "temperature_2m": 7.5,
                "precipitation": 0.4,
            }
        )
    }
    if status >= 400:
        res.raise_for_status.side_effect = requests.HTTPError(str(status))
    else:
        res.raise_for_status.return_value = None
    return res


@patch("app_radcomputer.views.requests.get")
class WindTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create(username="strava_4711")
        self.profile = StravaProfile.objects.create(
            user=self.user,
            strava_athlete_id=4711,
            access_token="acc-1",
            refresh_token="ref-1",
            expires_at=int(time.time()) + 3600,
            weather_consent_at=timezone.now(),
        )
        login_as(self.client, self.user, 4711)

    def wind(self, lat="51.23456", lon="6.78912"):
        return self.client.get(WIND_URL, {"lat": lat, "lon": lon})

    def test_returns_current_wind(self, get):
        get.return_value = open_meteo()
        res = self.wind()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            res.data,
            {
                "speed_kmh": 18.4,
                "gusts_kmh": 33.1,
                "temperature_c": 7.5,
                "precipitation_mm": 0.4,
                "direction_deg": 250.0,
                "observed_at": "2026-11-15T10:45Z",
                "lat": 51.23,
                "lon": 6.79,
            },
        )

    def test_sends_only_rounded_coordinates(self, get):
        get.return_value = open_meteo()
        self.wind()
        params = get.call_args.kwargs["params"]
        self.assertEqual((params["latitude"], params["longitude"]), (51.23, 6.79))
        self.assertEqual(params["wind_speed_unit"], "kmh")

    def test_without_consent_nothing_leaves_the_server(self, get):
        self.profile.weather_consent_at = None
        self.profile.save()
        res = self.wind()
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "weather_consent_required")
        get.assert_not_called()

    def test_nearby_positions_share_the_cache(self, get):
        get.return_value = open_meteo()
        self.wind(lat="51.2341", lon="6.7901")
        res = self.wind(lat="51.2349", lon="6.7898")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(get.call_count, 1)

    def test_rejects_missing_or_invalid_coordinates(self, get):
        for lat, lon in (("", "6.7"), ("abc", "6.7"), ("91", "6.7"), ("51", "181")):
            with self.subTest(lat=lat, lon=lon):
                self.assertEqual(self.wind(lat=lat, lon=lon).status_code, 400)
        self.assertEqual(self.client.get(WIND_URL).status_code, 400)
        get.assert_not_called()

    def test_open_meteo_failure_is_502_and_not_cached(self, get):
        get.side_effect = requests.ConnectionError("down")
        self.assertEqual(self.wind().status_code, 502)
        get.side_effect = None
        get.return_value = open_meteo()
        self.assertEqual(self.wind().status_code, 200)

    def test_http_error_and_missing_values_are_502(self, get):
        get.return_value = open_meteo(status=500)
        self.assertEqual(self.wind().status_code, 502)
        get.return_value = open_meteo(current={"time": "2026-11-15T10:45"})
        self.assertEqual(self.wind().status_code, 502)

    def test_missing_gusts_are_null(self, get):
        get.return_value = open_meteo(
            current={"wind_speed_10m": 5, "wind_direction_10m": 360}
        )
        res = self.wind()
        self.assertIsNone(res.data["gusts_kmh"])
        self.assertIsNone(res.data["observed_at"])
        self.assertEqual(res.data["direction_deg"], 0.0)

    def test_requires_login(self, get):
        self.client.logout()
        self.assertIn(self.wind().status_code, (401, 403))
        get.assert_not_called()


class RadcomputerLayoutSettingsTests(APITestCase):
    """Kartenstil und frei belegbare Datenseiten."""

    def setUp(self):
        self.user = User.objects.create(username="strava_4712")
        self.profile = StravaProfile.objects.create(
            user=self.user,
            strava_athlete_id=4712,
            access_token="a",
            refresh_token="r",
            expires_at=0,
        )
        login_as(self.client, self.user, 4712)

    def put(self, body):
        return self.client.put("/api/radcomputer/settings/", body, format="json")

    def test_defaults_to_cycling_map_and_client_layout(self):
        res = self.client.get("/api/radcomputer/settings/")
        self.assertEqual(res.data["map_style"], "cyclosm")
        self.assertEqual(res.data["data_pages"], [])

    def test_stores_pages_and_trims_names(self):
        pages = [
            {"name": " Tempo ", "fields": ["speed", "power3s", "hr"]},
            {"name": "Klingel", "fields": ["bell"]},
        ]
        res = self.put({"data_pages": pages, "map_style": "topo"})
        self.assertEqual(res.status_code, 200)
        stored = RadcomputerSettings.objects.get(profile=self.profile)
        self.assertEqual(stored.map_style, "topo")
        self.assertEqual(
            stored.data_pages[0], {"name": "Tempo", "fields": pages[0]["fields"]}
        )

    def test_unknown_field_keys_pass_so_new_client_fields_need_no_backend(self):
        res = self.put({"data_pages": [{"name": "Neu", "fields": ["somethingNew2"]}]})
        self.assertEqual(res.status_code, 200)

    def test_rejects_malformed_layouts(self):
        too_many_pages = [{"name": f"S{i}", "fields": ["speed"]} for i in range(9)]
        bad = [
            {"data_pages": "speed"},
            {"data_pages": too_many_pages},
            {"data_pages": [{"name": "Leer", "fields": []}]},
            {"data_pages": [{"name": "Viel", "fields": ["speed"] * 9}]},
            {"data_pages": [{"name": "", "fields": ["speed"]}]},
            {"data_pages": [{"name": "x" * 21, "fields": ["speed"]}]},
            {"data_pages": [{"name": "Böse", "fields": ["<script>"]}]},
            {"data_pages": [{"name": "Zahl", "fields": [3]}]},
            {"map_style": "satellite"},
        ]
        for body in bad:
            self.assertEqual(self.put(body).status_code, 400, body)


class ActivityCompleteTests(APITestCase):
    """Tags nach dem Upload nachtragen und die Fahrt sofort importieren."""

    def setUp(self):
        from app_maintenance.models import Bike, BikeType

        self.user = User.objects.create(username="strava_4713")
        self.profile = StravaProfile.objects.create(
            user=self.user,
            strava_athlete_id=4713,
            access_token="acc",
            refresh_token="ref",
            expires_at=int(time.time()) + 3600,
            scope="activity:read_all,activity:write",
        )
        self.bike = Bike.objects.create(
            athlete=self.profile,
            strava_bike_id="b77",
            name="Gravel",
            bike_type=BikeType.ROAD,
        )
        login_as(self.client, self.user, 4713)

    def complete(self, body=None):
        return self.client.post(
            "/api/radcomputer/activities/555/complete/", body or {}, format="json"
        )

    @patch("app_radcomputer.views.process_strava_webhook.delay")
    @patch("app_auth.api.utils.requests.request")
    def test_puts_tags_to_strava_and_queues_import(self, request, delay):
        request.return_value = fake(json={"id": 555, "athlete": {"id": 4713}})
        res = self.complete(
            {"sport_type": "GravelRide", "gear_id": "b77", "hide_from_home": True}
        )
        self.assertEqual(res.status_code, 200)
        method, url = request.call_args.args
        self.assertEqual(
            (method, url), ("PUT", "https://www.strava.com/api/v3/activities/555")
        )
        self.assertEqual(
            request.call_args.kwargs["json"],
            {"sport_type": "GravelRide", "gear_id": "b77", "hide_from_home": True},
        )
        self.assertEqual(
            res.data["applied"], ["gear_id", "hide_from_home", "sport_type"]
        )
        delay.assert_called_once_with(
            {"object_id": 555, "owner_id": 4713, "aspect_type": "create"}
        )

    @patch("app_radcomputer.views.process_strava_webhook.delay")
    @patch("app_auth.api.utils.requests.request")
    def test_without_tags_only_checks_and_imports(self, request, delay):
        request.return_value = fake(json={"id": 555})
        res = self.complete()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(request.call_args.args[0], "GET")
        delay.assert_called_once()

    @patch("app_auth.api.utils.requests.request")
    def test_rejects_bike_that_is_not_the_athletes(self, request):
        res = self.complete({"gear_id": "someone-elses"})
        self.assertEqual(res.status_code, 400)
        request.assert_not_called()

    def test_rejects_non_ride_sport_type(self):
        self.assertEqual(self.complete({"sport_type": "Run"}).status_code, 400)

    @patch("app_radcomputer.views.process_strava_webhook.delay")
    @patch("app_auth.api.utils.requests.request", return_value=fake(404))
    def test_foreign_activity_is_not_imported(self, _request, delay):
        self.assertEqual(self.complete().status_code, 404)
        delay.assert_not_called()

    @patch("app_radcomputer.views.recompute_weather_wear_for_bike.delay")
    @patch("app_radcomputer.views.process_strava_webhook.delay")
    @patch("app_auth.api.utils.requests.request")
    def test_already_imported_ride_moves_to_the_chosen_bike(
        self, request, delay, recompute
    ):
        from app_dashboard.models import Ride

        ride = Ride.objects.create(strava_id=555, name="Schon da", athlete=self.profile)
        request.return_value = fake(json={"id": 555})
        res = self.complete({"gear_id": "b77"})
        self.assertTrue(res.data["imported"])
        ride.refresh_from_db()
        self.assertEqual(ride.bike, self.bike)
        delay.assert_not_called()
        recompute.assert_called_once_with(self.bike.pk)

    def test_requires_write_scope(self):
        self.profile.scope = "activity:read_all"
        self.profile.save()
        res = self.complete()
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "scope_missing")

    @patch("app_auth.api.utils.requests.request")
    def test_upload_forwards_trainer_and_description(self, request):
        request.return_value = fake(201, {"id": 1, "status": "processing"})
        file = SimpleUploadedFile("f.tcx", b"<xml/>", content_type="application/xml")
        self.client.post(
            "/api/radcomputer/upload/",
            {
                "file": file,
                "data_type": "tcx",
                "trainer": "true",
                "description": " Nass ",
            },
            format="multipart",
        )
        data = request.call_args.kwargs["data"]
        self.assertEqual((data["trainer"], data["commute"]), ("1", "0"))
        self.assertEqual(data["description"], "Nass")
