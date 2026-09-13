from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="callback-site-tests-"))
os.environ["CALLBACK_APPDATA_DIR"] = str(TEST_DATA_DIR)

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import app as callback_app  # noqa: E402
from app import app  # noqa: E402
import core as callback_core  # noqa: E402


def _cleanup_test_data_dir() -> None:
    shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)


atexit.register(_cleanup_test_data_dir)


class BaseCallbackSiteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = app.test_client()
