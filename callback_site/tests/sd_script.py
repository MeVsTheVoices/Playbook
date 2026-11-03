from __future__ import annotations

import re
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
SD_SCRIPT = SCRIPTS_DIR / "prepare_sd_cloud_init.sh"
_SOURCE_LINE = re.compile(r'^# shellcheck source=lib/[\w.]+\nsource "\$SCRIPT_DIR/lib/([\w.]+)"\n', re.M)


def read_sd_script() -> str:
    """The SD prepare script with every sourced lib file inlined, in order, as one text."""
    return _SOURCE_LINE.sub(lambda m: (SCRIPTS_DIR / "lib" / m.group(1)).read_text(encoding="utf-8"), SD_SCRIPT.read_text(encoding="utf-8"))
