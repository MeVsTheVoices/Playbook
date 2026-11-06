from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from typing import Any, Dict

from flask import Flask

from launch import maybe_reexec_with_sudo
from routes.boot_logs import register_boot_log_routes
from routes.callbacks import register_callback_routes
from routes.connection_profiles import register_connection_profile_routes
from routes.images import catalog_entries_for_ui, register_image_routes
from routes.module_presets import register_module_preset_routes
from routes.monitor import register_monitor_routes
from routes.nodes import register_node_routes
from routes.openwrt import register_openwrt_routes
from routes.pages import register_page_routes
from routes.sd import register_sd_routes
from routes.settings import register_settings_routes
from routes.ssh import register_ssh_routes
from routes.test_terminal import register_test_terminal_routes
from services.kubernetes import _detect_kubernetes_runtime
from services.storage import APPDATA_DIR

# Setup logs directory
LOG_DIR = APPDATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "callback_site.log"

# Configure logging with rotation when the log file path is writable.
log_formatter = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log_handler = None
try:
    log_handler = RotatingFileHandler(
        str(LOG_FILE),
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=10,  # Keep 10 rotated files
    )
    log_handler.setFormatter(log_formatter)
except OSError:
    log_handler = None

# Get root logger and configure
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
if log_handler is not None:
    root_logger.addHandler(log_handler)

# Also log to console
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
root_logger.addHandler(console_handler)

logger = logging.getLogger(__name__)

app = Flask(__name__)


@app.context_processor
def inject_runtime_flags() -> Dict[str, Any]:
    kube_state = _detect_kubernetes_runtime(force_refresh=False)
    # Keep the page reachable once K3s is installed, even while the service is down.
    k3s_installed = bool(kube_state.get("k3s_binary") or kube_state.get("has_kubeconfig"))
    return {"kubernetes_available": bool(kube_state.get("available", False)) or k3s_installed}


register_page_routes(app, catalog_entries_for_ui=catalog_entries_for_ui)
register_openwrt_routes(app)
register_settings_routes(app, log_file=LOG_FILE, root_logger=root_logger, logger=logger)
register_callback_routes(app)
register_sd_routes(app)
register_image_routes(app)
register_ssh_routes(app)
register_test_terminal_routes(app)
register_monitor_routes(app)
register_node_routes(app)
register_module_preset_routes(app)
register_boot_log_routes(app)
register_connection_profile_routes(app)


if __name__ == "__main__":
    maybe_reexec_with_sudo()
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8088"))
    app.run(host=host, port=port, debug=False, threaded=True)
