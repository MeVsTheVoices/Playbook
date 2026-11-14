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
# Docker serves dashboard APIs on a separate local-only port (8089); the dev server uses one port.
CALLBACK_ADMIN_BASE = os.environ.get("CALLBACK_ADMIN_BASE", CALLBACK_BASE)
NODE_ONLY_MARKER = "only serves node callbacks"


def virtualenv_python() -> Path:
    if os.name == "nt":
        return ROOT_DIR / ".venv" / "Scripts" / "python.exe"
    return ROOT_DIR / ".venv" / "bin" / "python"


def require_endpoint(path: str, base: str | None = None) -> bool:
    base = base or CALLBACK_BASE
    body = ""
    try:
        with urlopen(f"{base}{path}", timeout=10) as response:
            code = response.status
    except HTTPError as error:
        code = error.code
        try:
            body = error.read(4096).decode("utf-8", "replace")
        except OSError:
            body = ""
    except URLError as error:
        print(f"ERROR {path} request failed: {error.reason}")
        return False

    if code == 200:
        print(f"OK {path}")
        return True

    if code == 401:
        print(f"OK {path} (reachable; dashboard sign-in required)")
        return True

    if code == 404 and NODE_ONLY_MARKER in body:
        print(f"ERROR {path} hit the node-only port {base}.")
        print("Set CALLBACK_ADMIN_BASE=http://127.0.0.1:8089 for the Docker dashboard port.")
        return False

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
    if not require_endpoint("/health"):
        return 1
    for path in ("/api/cloud-init/events", "/api/test-terminal/checks"):
        if not require_endpoint(path, CALLBACK_ADMIN_BASE):
            return 1

    print("[3/3] Posting a synthetic callback event", flush=True)
    if not post_synthetic_callback():
        return 1

    print("Validation complete: unit tests and callback APIs are healthy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
