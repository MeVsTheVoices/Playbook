#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT_DIR = Path(__file__).resolve().parents[1]
CALLBACK_BASE = os.environ.get("CALLBACK_BASE", "http://127.0.0.1:8088")


def virtualenv_python() -> Path:
    if os.name == "nt":
        return ROOT_DIR / ".venv" / "Scripts" / "python.exe"
    return ROOT_DIR / ".venv" / "bin" / "python"


def require_endpoint(path: str) -> bool:
    try:
        with urlopen(f"{CALLBACK_BASE}{path}", timeout=10) as response:
            code = response.status
    except HTTPError as error:
        code = error.code
    except URLError as error:
        print(f"ERROR {path} request failed: {error.reason}")
        return False

    if code == 200:
        print(f"OK {path}")
        return True

    if code == 404 and path == "/api/test-terminal/checks":
        print(
            f"ERROR {path} returned 404. "
            "The callback service is likely running an older build."
        )
        print("Restart callback_site and re-run this script.")
        return False

    print(f"ERROR {path} returned HTTP {code}.")
    return False


def post_synthetic_callback() -> bool:
    payload = json.dumps(
        {
            "hostname": "preflight-node",
            "instance_id": "preflight-01",
            "stage": "phone_home",
            "profile": "pi5",
        }
    ).encode("utf-8")
    request = Request(
        f"{CALLBACK_BASE}/api/cloud-init/callback",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            code = response.status
    except HTTPError as error:
        print(f"ERROR callback POST returned HTTP {error.code}.")
        return False
    except URLError as error:
        print(f"ERROR callback POST request failed: {error.reason}")
        return False

    if 200 <= code < 300:
        return True
    print(f"ERROR callback POST returned HTTP {code}.")
    return False


def main() -> int:
    argparse.ArgumentParser(
        description="Run callback-site unit and endpoint preflight checks."
    ).parse_args()
    python = virtualenv_python()
    print("[1/3] Running callback_site unit tests", flush=True)
    if not python.is_file():
        print(f"ERROR: {python} not found. Run scripts/bootstrap_project.sh first.")
        return 1

    result = subprocess.run(
        [
            str(python),
            "-m",
            "unittest",
            "discover",
            "-s",
            str(ROOT_DIR / "callback_site" / "tests"),
            "-p",
            "test_*.py",
        ],
        check=False,
    )
    if result.returncode:
        return result.returncode

    print("[2/3] Checking local callback endpoints", flush=True)
    for path in ("/health", "/api/cloud-init/events", "/api/test-terminal/checks"):
        if not require_endpoint(path):
            return 1

    print("[3/3] Posting a synthetic callback event", flush=True)
    if not post_synthetic_callback():
        return 1

    print("Validation complete: unit tests and callback APIs are healthy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
