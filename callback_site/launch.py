from __future__ import annotations

import os
import shutil
import sys
from typing import Sequence


def _is_truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def should_require_root() -> bool:
    return _is_truthy(os.getenv("CALLBACK_SITE_REQUIRE_ROOT"))


def build_sudo_command(argv: Sequence[str] | None = None, python_executable: str | None = None) -> list[str]:
    sudo_path = shutil.which("sudo")
    if not sudo_path:
        raise RuntimeError("CALLBACK_SITE_REQUIRE_ROOT is set, but sudo is not available on PATH")

    script_argv = list(argv if argv is not None else sys.argv)
    executable = python_executable or sys.executable
    return [sudo_path, "-E", executable, *script_argv]


def maybe_reexec_with_sudo() -> None:
    if not should_require_root():
        return
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return

    command = build_sudo_command()
    os.execvp(command[0], command)