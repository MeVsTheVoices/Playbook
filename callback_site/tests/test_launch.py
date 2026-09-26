from __future__ import annotations

import os
from unittest.mock import patch

from test_support import BaseCallbackSiteTest

import launch


class LaunchTests(BaseCallbackSiteTest):
    def test_build_sudo_command_uses_python_executable_and_preserves_argv(self) -> None:
        with patch("launch.shutil.which", return_value="/usr/bin/sudo"):
            command = launch.build_sudo_command(["callback_site/app.py"], "/opt/python/bin/python3")

        self.assertEqual(
            command,
            ["/usr/bin/sudo", "-E", "/opt/python/bin/python3", "callback_site/app.py"],
        )

    def test_maybe_reexec_with_sudo_is_noop_when_not_requested(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            with patch("launch.os.execvp") as execvp:
                launch.maybe_reexec_with_sudo()

        execvp.assert_not_called()

    def test_maybe_reexec_with_sudo_calls_execvp_when_root_is_required(self) -> None:
        with patch.dict(os.environ, {"CALLBACK_SITE_REQUIRE_ROOT": "1"}, clear=False):
            with patch("launch.os.geteuid", return_value=1000), patch("launch.shutil.which", return_value="/usr/bin/sudo"), patch("launch.os.execvp") as execvp:
                launch.maybe_reexec_with_sudo()

        execvp.assert_called_once()