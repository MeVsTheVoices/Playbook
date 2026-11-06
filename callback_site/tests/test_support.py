from __future__ import annotations

import atexit
import importlib
import os
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Iterable
from unittest.mock import MagicMock, patch

TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="callback-site-tests-"))
os.environ["CALLBACK_APPDATA_DIR"] = str(TEST_DATA_DIR)
# Tests post callbacks constantly; a background SSH pull per node would only add noise.
os.environ["CALLBACK_AUTO_BOOT_LOGS"] = "0"

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import app as callback_app  # noqa: E402
from app import app  # noqa: E402


def _cleanup_test_data_dir() -> None:
    shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)


atexit.register(_cleanup_test_data_dir)


class patch_in:
    """Replace one name with a single shared mock in every loaded project module that holds it.

    A from-import copies a name into each importing module, so patching only the defining module
    misses its callers. Pass `modules` to limit the patch. Usable as a context manager or via start()/stop().
    """

    def __init__(self, name: str, modules: Iterable[str] | None = None, **mock_kwargs: Any) -> None:
        self.mock = MagicMock(**mock_kwargs)
        if modules is None:
            modules = [
                key for key, mod in list(sys.modules.items())
                if mod is not None and (key == "app" or key.startswith(("services.", "routes."))) and hasattr(mod, name)
            ]
        self._targets = [(importlib.import_module(module), name) for module in modules]
        self._stack: ExitStack | None = None

    def start(self) -> MagicMock:
        self._stack = ExitStack()
        for module, name in self._targets:
            self._stack.enter_context(patch.object(module, name, self.mock))
        return self.mock

    def stop(self) -> None:
        if self._stack is not None:
            self._stack.close()
            self._stack = None

    def __enter__(self) -> MagicMock:
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()


class BaseCallbackSiteTest(unittest.TestCase):
    def page_text(self, response) -> str:
        """The page HTML plus the local stylesheets and scripts it links, for assertions on inline-era content."""
        html = response.get_data(as_text=True)
        parts = [html]
        for path in re.findall(r'(?:src|href)="(/static/[^"?#]+\.(?:js|css))', html):
            parts.append(self.client.get(path).get_data(as_text=True))
        return "\n".join(parts)

    def setUp(self) -> None:
        self.client = app.test_client()
        # The Node Ops hardware check POSTs here; port 9 is closed, so tests can never reach the live site.
        guard = patch_in(
            "_default_callback_url",
            ["services.node_runs"],
            return_value="http://127.0.0.1:9/api/cloud-init/callback",
        )
        guard.start()
        self.addCleanup(guard.stop)
        # The real value comes from the K3s on this machine, which would make env assertions machine-dependent.
        version_guard = patch_in("_local_k3s_version", return_value="")
        version_guard.start()
        self.addCleanup(version_guard.stop)
        # This machine's tailnet would otherwise change which address a node resolves to.
        tailnet_guard = patch_in("_tailscale_peer_ip", return_value="")
        tailnet_guard.start()
        self.addCleanup(tailnet_guard.stop)
