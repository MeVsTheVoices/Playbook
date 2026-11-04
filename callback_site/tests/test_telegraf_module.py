"""Telegraf module in Node Ops: token form, saved secrets in the connection test, and the scrollable module list."""

from __future__ import annotations

import io
from unittest.mock import patch
from urllib import error as urlerror

try:
    from .test_support import BaseCallbackSiteTest
except ImportError:
    from test_support import BaseCallbackSiteTest

import routes.nodes
import services.node_runs
from domain_defaults import NODE_PROVISION_PROFILES

INPUTS = {
    "influx_endpoint": "https://influx.example:8086/",
    "influx_bucket": "telegraf",
    "influx_org": "homelab",
    "influx_access_key": "access",
    "influx_secret_key": "secret",
}


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def fake_influx(accepted_token: str):
    """health always answers; writes succeed only with `accepted_token`."""

    def opener(req, timeout=None):  # noqa: ANN001
        if req.full_url.endswith("/health"):
            return FakeResponse(200)
        if req.get_header("Authorization") == f"Token {accepted_token}":
            return FakeResponse(204)
        raise urlerror.HTTPError(req.full_url, 401, "unauthorized", {}, io.BytesIO(b"bad token"))

    return opener


class TelegrafModuleTests(BaseCallbackSiteTest):
    def telegraf_specs(self) -> dict:
        profile = next(p for p in NODE_PROVISION_PROFILES if p["id"] == "telegraf")
        return {s["id"]: s for s in profile["input_specs"]}

    def test_token_form_is_a_select_and_the_secret_is_optional(self) -> None:
        specs = self.telegraf_specs()
        self.assertEqual(specs["influx_token_mode"]["input_type"], "select")
        self.assertEqual(specs["influx_token_mode"]["options"], ["access_key", "access_key:secret_key"])
        self.assertEqual(specs["influx_token_mode"]["default"], "access_key")
        self.assertFalse(specs["influx_secret_key"]["required"])
        self.assertTrue(specs["influx_secret_key"]["sensitive"])

    def test_connection_test_reports_the_token_form_that_worked(self) -> None:
        with patch.object(services.node_runs.urlrequest, "urlopen", fake_influx("access:secret")):
            result = services.node_runs._telegraf_influx_dummy_check(dict(INPUTS))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["auth_mode"], "access_key:secret_key")

        with patch.object(services.node_runs.urlrequest, "urlopen", fake_influx("access")):
            result = services.node_runs._telegraf_influx_dummy_check(dict(INPUTS))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["auth_mode"], "access_key")

    def test_connection_test_needs_no_secret_for_the_plain_token_form(self) -> None:
        inputs = {**INPUTS, "influx_secret_key": ""}
        with patch.object(services.node_runs.urlrequest, "urlopen", fake_influx("access")):
            result = services.node_runs._telegraf_influx_dummy_check(inputs)
        self.assertTrue(result["ok"], result)

        with patch.object(services.node_runs.urlrequest, "urlopen", fake_influx("something-else")):
            failed = services.node_runs._telegraf_influx_dummy_check(inputs)
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["auth_mode"], "")

    def test_blank_secret_fields_keep_the_saved_secret_but_other_blanks_stand(self) -> None:
        state = {"node_inputs": {"n1": {"telegraf": {**INPUTS, "influx_access_key": "saved-access", "influx_secret_key": "saved-secret"}}}}
        merged = routes.nodes._merged_telegraf_inputs(
            state, "n1", {"telegraf": {"influx_endpoint": "", "influx_bucket": "other", "influx_access_key": "", "influx_secret_key": ""}}
        )
        self.assertEqual(merged["influx_access_key"], "saved-access")
        self.assertEqual(merged["influx_secret_key"], "saved-secret")
        self.assertEqual(merged["influx_bucket"], "other")
        self.assertEqual(merged["influx_endpoint"], "", "a blank endpoint must still be reported as missing")

        typed = routes.nodes._merged_telegraf_inputs(state, "n1", {"telegraf": {"influx_access_key": "new"}})
        self.assertEqual(typed["influx_access_key"], "new")

    def test_test_endpoint_returns_the_auth_mode(self) -> None:
        name = "telegraf-test-node"
        self.assertEqual(self.client.post("/api/nodes/manual-nodes", json={"name": name, "certificate": "test-cert"}).status_code, 200)
        self.addCleanup(self.client.delete, f"/api/nodes/manual-nodes/{name}")
        with patch.object(services.node_runs.urlrequest, "urlopen", fake_influx("access:secret")):
            response = self.client.post("/api/nodes/telegraf/test", json={"node": name, "profile_inputs": {"telegraf": INPUTS}})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["result"]["auth_mode"], "access_key:secret_key")

    def test_nodes_page_has_the_test_button_and_a_scrollable_module_list(self) -> None:
        text = self.page_text(self.client.get("/nodes"))
        self.assertIn('id="module-list-scroll"', text)
        self.assertIn(".module-list-scroll", text)
        self.assertIn("overflow-y: auto", text)
        self.assertIn("Test Influx connection", text)
        self.assertIn("testInfluxConnection", text)
