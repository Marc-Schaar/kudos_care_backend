import time
from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APITestCase

from app_auth.models import StravaProfile


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
