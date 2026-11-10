"""Dashboard sign-in, first-run setup, and the node-only listener."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from test_support import TEST_ADMIN_PASSWORD, TEST_ADMIN_USERNAME, BaseCallbackSiteTest, app
from services import admin_auth

PASSWORD = "correct horse battery"


def _csrf(response) -> str:
    match = re.search(r'name="csrf_token" value="([0-9a-f]{64})"', response.get_data(as_text=True))
    assert match, response.get_data(as_text=True)[:500]
    return match.group(1)


class FreshInstallTests(BaseCallbackSiteTest):
    def setUp(self) -> None:
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.auth_file = Path(directory.name) / "admin_auth.json"
        guard = patch.object(admin_auth, "AUTH_FILE", self.auth_file)
        guard.start()
        self.addCleanup(guard.stop)
        self.addCleanup(admin_auth.reset_login_backoff)
        self.client = app.test_client(authenticated=False)

    def setup_account(self, client=None, **form):
        client = client or self.client
        token = _csrf(client.get("/setup"))
        data = {"csrf_token": token, "username": "rack-admin", "password": PASSWORD, "confirm": PASSWORD, **form}
        return client.post("/setup", data=data)

    def test_uninitialized_dashboard_sends_everyone_to_setup_but_nodes_still_report(self) -> None:
        page = self.client.get("/nodes")
        self.assertEqual(page.status_code, 302)
        self.assertTrue(page.headers["Location"].endswith("/setup"))
        api = self.client.get("/api/cloud-init/events")
        self.assertEqual(api.status_code, 401)
        self.assertEqual(api.get_json()["setup_url"], "/setup")
        self.assertEqual(self.client.post("/api/sd/prepare", json={}).status_code, 401)
        self.assertEqual(self.client.get("/login").status_code, 302)
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.post("/api/cloud-init/callback", json={}).get_json(), {"ok": True, "ignored": True})
        self.assertNotEqual(self.client.post("/api/cloud-init/boot-log", json={}).status_code, 401)
        with self.client.get("/static/common.css") as stylesheet:
            self.assertEqual(stylesheet.status_code, 200)

    def test_setup_creates_one_hashed_account_and_signs_in(self) -> None:
        page = self.client.get("/setup")
        self.assertEqual(page.status_code, 200)
        self.assertIn("First run", page.get_data(as_text=True))
        self.assertIn("/static/auth.css", page.get_data(as_text=True))
        self.assertEqual(self.setup_account(confirm="different password").status_code, 400)
        self.assertEqual(self.setup_account(password="short", confirm="short").status_code, 400)
        self.assertEqual(self.setup_account(username="-rm").status_code, 400)
        self.assertEqual(
            self.client.post("/setup", data={"username": "x", "password": PASSWORD, "confirm": PASSWORD}).status_code,
            400,
        )
        self.assertFalse(self.auth_file.exists())

        created = self.setup_account()
        self.assertEqual(created.status_code, 302)
        self.assertEqual(created.headers["Location"], "/")
        cookie = self.client.get_cookie("playbook_admin")
        self.assertTrue(cookie.http_only)
        self.assertEqual(cookie.same_site, "Strict")
        self.assertIsNotNone(cookie.expires)
        dashboard = self.client.get("/")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn('action="/logout"', dashboard.get_data(as_text=True))

        stored = self.auth_file.read_text()
        self.assertNotIn(PASSWORD, stored)
        self.assertEqual(json.loads(stored)["username"], "rack-admin")
        if os.name != "nt":
            self.assertEqual(self.auth_file.stat().st_mode & 0o777, 0o600)

        intruder = app.test_client(authenticated=False)
        self.assertEqual(intruder.get("/setup").status_code, 302)
        self.assertEqual(intruder.post("/setup", data={"username": "evil", "password": PASSWORD}).status_code, 302)
        self.assertEqual(json.loads(self.auth_file.read_text())["username"], "rack-admin")

    def test_setup_refuses_hostnames_a_rebinding_page_could_use(self) -> None:
        hostile = self.client.get("/setup", base_url="http://attacker.example:8089")
        self.assertEqual(hostile.status_code, 403)
        self.assertNotIn("csrf_token", hostile.get_data(as_text=True))
        self.assertEqual(self.client.get("/setup", base_url="http://192.168.1.20:8089").status_code, 200)
        self.assertEqual(self.client.get("/setup", base_url="http://localhost:8089").status_code, 200)
        with patch.dict(os.environ, {"CALLBACK_ADMIN_HOSTS": "nucbox.tail1234.ts.net"}):
            self.assertEqual(self.client.get("/setup", base_url="https://nucbox.tail1234.ts.net").status_code, 200)

    def test_login_logout_redirects_and_backoff(self) -> None:
        self.setup_account()
        self.client.post("/logout")
        page = self.client.get("/nodes?tab=ops")
        self.assertEqual(page.status_code, 302)
        location = urlsplit(page.headers["Location"])
        self.assertEqual(location.path, "/login")
        self.assertEqual(parse_qs(location.query)["next"], ["/nodes?tab=ops"])
        self.assertEqual(self.client.get("/api/nodes/summary").status_code, 401)

        token = _csrf(self.client.get("/login"))
        wrong = self.client.post("/login", data={"csrf_token": token, "username": "rack-admin", "password": "nope"})
        self.assertEqual(wrong.status_code, 401)
        self.assertIn("Wrong username or password", wrong.get_data(as_text=True))
        self.assertEqual(self.client.post("/login", data={"username": "rack-admin", "password": PASSWORD}).status_code, 400)
        hostile_next = self.client.post("/login", data={
            "csrf_token": token, "username": "rack-admin", "password": PASSWORD, "next": "//attacker.example/",
        })
        self.assertEqual(hostile_next.status_code, 302)
        self.assertEqual(hostile_next.headers["Location"], "/")
        self.assertEqual(self.client.get("/nodes").status_code, 200)

        self.client.post("/logout")
        token = _csrf(self.client.get("/login"))
        for _ in range(admin_auth.FREE_LOGIN_ATTEMPTS + 1):
            self.client.post("/login", data={"csrf_token": token, "username": "rack-admin", "password": "nope"})
        blocked = self.client.post("/login", data={"csrf_token": token, "username": "rack-admin", "password": PASSWORD})
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(self.client.get("/nodes").status_code, 302)

    def test_recreating_the_account_signs_out_old_sessions(self) -> None:
        self.setup_account()
        self.assertEqual(self.client.get("/").status_code, 200)
        self.auth_file.unlink()
        admin_auth.create_account("rack-admin", PASSWORD)
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_corrupt_account_file_fails_closed(self) -> None:
        self.auth_file.write_text("{not json")
        response = self.client.get("/api/cloud-init/events")
        self.assertEqual(response.status_code, 503)
        self.assertIn("delete it", response.get_json()["error"])
        self.assertEqual(self.client.post("/api/cloud-init/callback", json={}).status_code, 200)


class NodeOnlyPortTests(BaseCallbackSiteTest):
    def setUp(self) -> None:
        super().setUp()
        guard = patch.dict(os.environ, {"CALLBACK_NODE_ONLY_PORT": "8088", "CALLBACK_ADMIN_PORT": "8089"})
        guard.start()
        self.addCleanup(guard.stop)

    def test_node_port_serves_only_node_endpoints_even_when_signed_in(self) -> None:
        # The signed-in cookie is scoped to localhost; cookies ignore ports, so it reaches 8088 too.
        node = "http://localhost:8088"
        for method, path in (("get", "/"), ("get", "/setup"), ("get", "/login"), ("get", "/api/cloud-init/events"),
                             ("post", "/api/sd/prepare"), ("get", "/static/common.css")):
            with self.subTest(path=path):
                response = getattr(self.client, method)(path, base_url=node)
                self.assertEqual(response.status_code, 404)
                self.assertIn("port 8089", response.get_data(as_text=True))
        self.assertEqual(self.client.get("/health", base_url=node).status_code, 200)
        self.assertEqual(self.client.post("/api/cloud-init/callback", json={}, base_url=node).status_code, 200)
        public_key = self.client.get("/api/ssh/public/no-such-key", base_url=node)
        self.assertNotIn("port 8089", public_key.get_data(as_text=True))
        self.assertNotIn("8089", self.client.post("/api/cloud-init/boot-log", json={}, base_url=node).get_data(as_text=True))

    def test_admin_port_serves_the_signed_in_dashboard(self) -> None:
        self.assertEqual(self.client.get("/", base_url="http://localhost:8089").status_code, 200)
        anonymous = app.test_client(authenticated=False)
        self.assertEqual(anonymous.get("/", base_url="http://localhost:8089").status_code, 302)

    def test_default_dashboard_guidance_avoids_the_host_flasher_port(self) -> None:
        with patch.dict(os.environ):
            os.environ.pop("CALLBACK_ADMIN_PORT", None)
            response = self.client.get("/", base_url="http://localhost:8088")
        self.assertEqual(response.status_code, 404)
        self.assertIn("port 8091", response.get_data(as_text=True))


class SignedInHarnessTests(BaseCallbackSiteTest):
    def test_shared_harness_account_signs_in_with_its_password(self) -> None:
        self.addCleanup(admin_auth.reset_login_backoff)
        client = app.test_client(authenticated=False)
        token = _csrf(client.get("/login"))
        response = client.post("/login", data={
            "csrf_token": token, "username": TEST_ADMIN_USERNAME, "password": TEST_ADMIN_PASSWORD,
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(client.get("/").status_code, 200)

    def test_secret_key_is_private_and_stable(self) -> None:
        self.assertEqual(admin_auth.load_secret_key(), app.config["SECRET_KEY"])
        if os.name != "nt":
            self.assertEqual(admin_auth.SECRET_KEY_FILE.stat().st_mode & 0o777, 0o600)
