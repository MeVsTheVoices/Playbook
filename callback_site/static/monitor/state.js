const body = document.body;

const toggleBtn = document.getElementById("sidebar-toggle");

const monitorStatus = document.getElementById("monitor-status");

const pruneStaleMonitorBtn = document.getElementById("prune-stale-monitor");

const nodeFormStatusEl = document.getElementById("node-form-status");

const certNodeEl = document.getElementById("cert-node");

const certStatusEl = document.getElementById("cert-status");

const certOutputEl = document.getElementById("cert-output");

const hwBodyEl = document.getElementById("hw-body");

const monitorNodeSelectEl = document.getElementById("monitor-node-select");

const monitorPingNodeSelectEl = document.getElementById("monitor-ping-node-select");

const monitorRunCheckBtn = document.getElementById("monitor-run-check");

const summaryRunPingBtn = document.getElementById("summary-run-ping");

const monitorRunPingBtn = document.getElementById("monitor-run-ping");

const nodeFormPingBtn = document.getElementById("node-form-ping");

const monitorCheckStatusEl = document.getElementById("monitor-check-status");

const sotKpisEl = document.getElementById("sot-kpis");

const sotBodyEl = document.getElementById("sot-body");

const sotMetaEl = document.getElementById("sot-meta");

const fleetKpisEl = document.getElementById("fleet-kpis");

const hardwareKpisEl = document.getElementById("hardware-kpis");

const cloudInitCoverageKpisEl = document.getElementById("cloud-init-coverage-kpis");

const monitorCloudInitBodyEl = document.getElementById("monitor-cloud-init-body");

const monitorOptionCatalogEl = document.getElementById("monitor-option-catalog");

const latestCheckKpisEl = document.getElementById("latest-check-kpis");

const latestChecksBodyEl = document.getElementById("latest-checks-body");

let nodes = [];

let sourceOfTruthState = null;

const pingResetTimers = new WeakMap();

const PING_RESET_DELAYS_MS = Object.freeze({
  "ping-good": 1500,
  "ping-warn": 4500,
  "ping-bad": 9000,
  default: 2000,
});

function initSidebar() {
  const saved = localStorage.getItem("sidebar-collapsed");
  if (saved === "1") body.classList.add("sidebar-collapsed");
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function compactPathLabel(value) {
  const text = String(value || "").trim();
  if (!text || !text.includes("/")) {
    return text;
  }
  const parts = text.split("/").filter(Boolean);
  if (parts.length < 2) {
    return text;
  }
  return `${parts[parts.length - 2]}/${parts[parts.length - 1]}`;
}

function formatTimestamp(value) {
  const text = String(value || "").trim();
  if (!text || text === "-") {
    return "-";
  }
  const dt = new Date(text);
  if (Number.isNaN(dt.getTime())) {
    return text;
  }
  return dt.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
    timeZoneName: "short",
  });
}
