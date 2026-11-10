"""Tests for the callback-site preflight HTTP checks."""

from __future__ import annotations

from contextlib import redirect_stdout
from io import BytesIO, StringIO
import importlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
validate_callback_site = importlib.import_module("validate_callback_site")


class ValidateCallbackSiteTests(unittest.TestCase):
    def test_require_endpoint_accepts_http_200(self) -> None:
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.status = 200
        output = StringIO()
        with patch.object(validate_callback_site, "urlopen", return_value=response):
            with redirect_stdout(output):
                result = validate_callback_site.require_endpoint("/health")
        self.assertTrue(result)
        self.assertIn("OK /health", output.getvalue())

    def test_terminal_check_404_explains_old_service(self) -> None:
        error = HTTPError("http://localhost", 404, "Not Found", {}, BytesIO())
        output = StringIO()
        with patch.object(validate_callback_site, "urlopen", side_effect=error):
            with redirect_stdout(output):
                result = validate_callback_site.require_endpoint(
                    "/api/test-terminal/checks"
                )
        self.assertFalse(result)
        self.assertIn("older build", output.getvalue())
        self.assertIn("Restart callback_site", output.getvalue())

    def test_signed_out_dashboard_api_counts_as_reachable(self) -> None:
        error = HTTPError("http://localhost", 401, "Unauthorized", {}, BytesIO(b'{"ok": false}'))
        output = StringIO()
        with patch.object(validate_callback_site, "urlopen", side_effect=error):
            with redirect_stdout(output):
                result = validate_callback_site.require_endpoint("/api/cloud-init/events")
        self.assertTrue(result)
        self.assertIn("sign-in required", output.getvalue())

    def test_node_only_port_404_points_at_the_dashboard_port(self) -> None:
        body = b"This port only serves node callbacks. Open the dashboard on port 8089."
        error = HTTPError("http://localhost", 404, "Not Found", {}, BytesIO(body))
        output = StringIO()
        with patch.object(validate_callback_site, "urlopen", side_effect=error) as urlopen:
            with redirect_stdout(output):
                result = validate_callback_site.require_endpoint(
                    "/api/test-terminal/checks", "http://127.0.0.1:8088"
                )
        self.assertFalse(result)
        self.assertEqual(urlopen.call_args.args[0], "http://127.0.0.1:8088/api/test-terminal/checks")
        self.assertIn("CALLBACK_ADMIN_BASE", output.getvalue())
        self.assertNotIn("older build", output.getvalue())

    def test_require_endpoint_reports_connection_errors(self) -> None:
        output = StringIO()
        with patch.object(
            validate_callback_site,
            "urlopen",
            side_effect=URLError("connection refused"),
        ):
            with redirect_stdout(output):
                result = validate_callback_site.require_endpoint("/health")
        self.assertFalse(result)
        self.assertIn("connection refused", output.getvalue())

    def test_post_synthetic_callback_sends_expected_json(self) -> None:
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.status = 201
        with patch.object(
            validate_callback_site, "urlopen", return_value=response
        ) as urlopen:
            self.assertTrue(validate_callback_site.post_synthetic_callback())
        request = urlopen.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(
            json.loads(request.data),
            {
                "hostname": "preflight-node",
                "instance_id": "preflight-01",
                "stage": "phone_home",
                "profile": "pi5",
            },
        )


if __name__ == "__main__":
    unittest.main()
