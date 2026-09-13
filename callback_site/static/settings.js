const body = document.body;
const toggleBtn = document.getElementById("sidebar-toggle");
const statusBox = document.getElementById("settings-status");
const resetNodeOpsBtn = document.getElementById("reset-node-ops");
const clearCallbackBtn = document.getElementById("clear-callback-backlog");
const callbackClearStatus = document.getElementById("callback-clear-status");
const rackNameInput = document.getElementById("rack-name");
const saveRackNameBtn = document.getElementById("save-rack-name");
const rackStatus = document.getElementById("rack-status");
const checkCatalogHealthBtn = document.getElementById("check-catalog-health");
const catalogHealthStatus = document.getElementById("catalog-health-status");
const enableVerboseBtn = document.getElementById("enable-verbose-logging");
const disableVerboseBtn = document.getElementById("disable-verbose-logging");
const loggingStatus = document.getElementById("logging-status");
const loggingOutput = document.getElementById("logging-output");
const refreshModuleSyncBtn = document.getElementById("refresh-module-sync");
const moduleSyncStatus = document.getElementById("module-sync-status");
const moduleSyncGrid = document.getElementById("module-sync-grid");
const staleMaxAgeHoursInput = document.getElementById("stale-max-age-hours");
const previewStalePruneBtn = document.getElementById("preview-stale-prune");
const runStalePruneBtn = document.getElementById("run-stale-prune");
const stalePruneStatus = document.getElementById("stale-prune-status");

function initSidebar() {
  const saved = localStorage.getItem("sidebar-collapsed");
  if (saved === "1") {
    body.classList.add("sidebar-collapsed");
  }
}

toggleBtn.addEventListener("click", () => {
  body.classList.toggle("sidebar-collapsed");
  localStorage.setItem("sidebar-collapsed", body.classList.contains("sidebar-collapsed") ? "1" : "0");
});

async function resetNodeOps() {
  const confirmed = window.confirm("Reset Node Ops state and delete stored SSH key/certificate files?");
  if (!confirmed) {
    return;
  }

  statusBox.textContent = "Resetting node ops settings...";
  const res = await fetch("/api/settings/reset", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scope: "node_ops" }),
  });
  const data = await res.json();
  if (!res.ok || !data.ok) {
    statusBox.textContent = data.error || "Reset failed.";
    return;
  }
  statusBox.textContent = `Reset complete: ${JSON.stringify(data.reset, null, 2)}`;
}

async function clearCallbackBacklog() {
  const confirmed = window.confirm("Clear all stored callback events?");
  if (!confirmed) {
    return;
  }

  callbackClearStatus.textContent = "Clearing callback backlog...";
  const res = await fetch("/api/cloud-init/events/clear", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  const data = await res.json();
  if (!res.ok || !data.ok) {
    callbackClearStatus.textContent = data.error || "Clear failed.";
    return;
  }
  callbackClearStatus.textContent = `Cleared events: ${data.cleared}`;
}

async function loadRackSettings() {
  rackStatus.textContent = "Loading rack settings...";
  const res = await fetch("/api/settings");
  const data = await res.json();
  if (!res.ok || !data.ok) {
    rackStatus.textContent = data.error || "Failed to load rack settings.";
    return;
  }
  const rackName = (data.settings || {}).rack_name || "homelab";
  const verboseEnabled = Boolean(data.settings && data.settings.verbose_logging);
  rackNameInput.value = rackName;
  rackStatus.textContent = `Current rack name: ${rackName}`;
  updateVerboseButtons(verboseEnabled);
  loggingStatus.textContent = verboseEnabled
    ? "Verbose logging is currently enabled."
    : "Verbose logging is currently disabled.";
}

function updateVerboseButtons(enabled) {
  enableVerboseBtn.disabled = Boolean(enabled);
  disableVerboseBtn.disabled = !enabled;
}

function renderModuleSyncCard(title, value, detail, state) {
  return `<article class="module-card ${state}"><p class="module-k">${title}</p><p class="module-v">${value}</p><p class="module-d">${detail}</p></article>`;
}

async function refreshModuleSync() {
  if (!moduleSyncStatus || !moduleSyncGrid) {
    return;
  }

  moduleSyncStatus.textContent = "Refreshing module snapshot...";
  const requests = await Promise.allSettled([
    fetch("/api/sd/catalog"),
    fetch("/api/sd/options-profiles"),
    fetch("/api/sd/devices"),
    fetch("/api/cloud-init/events"),
    fetch("/api/settings"),
  ]);

  const [catalogRes, profilesRes, devicesRes, eventsRes, settingsRes] = requests;
  const failed = requests.filter((entry) => entry.status !== "fulfilled").length;

  if (failed > 0) {
    moduleSyncStatus.textContent = `Snapshot loaded with ${failed} request error${failed === 1 ? "" : "s"}.`;
  }

  const catalogData =
    catalogRes.status === "fulfilled" && catalogRes.value.ok ? await catalogRes.value.json() : { entries: [] };
  const profilesData =
    profilesRes.status === "fulfilled" && profilesRes.value.ok ? await profilesRes.value.json() : { profiles: [] };
  const devicesData =
    devicesRes.status === "fulfilled" && devicesRes.value.ok ? await devicesRes.value.json() : { devices: [], errors: [] };
  const eventsData =
    eventsRes.status === "fulfilled" && eventsRes.value.ok ? await eventsRes.value.json() : { events: [] };
  const settingsData =
    settingsRes.status === "fulfilled" && settingsRes.value.ok ? await settingsRes.value.json() : { settings: {} };

  const catalogEntries = Array.isArray(catalogData.entries) ? catalogData.entries : [];
  const catalogAvailable = catalogEntries.filter((entry) => Boolean(entry.local_available)).length;
  const catalogDownloadable = catalogEntries.filter((entry) => String(entry.image_url || "").trim()).length;

  const profiles = Array.isArray(profilesData.profiles) ? profilesData.profiles : [];
  const tailscaleProfiles = profiles.filter((profile) => Boolean(profile.options && profile.options.tailscale_enable)).length;

  const devices = Array.isArray(devicesData.devices) ? devicesData.devices : [];
  const deviceErrors = Array.isArray(devicesData.errors) ? devicesData.errors.length : 0;

  const events = Array.isArray(eventsData.events) ? eventsData.events : [];
  const eventsWithProfile = events.filter((event) => String(event.profile || "").trim()).length;
  const rackName = ((settingsData.settings || {}).rack_name || "homelab").trim() || "homelab";

  const cards = [
    renderModuleSyncCard(
      "Image Catalog",
      `${catalogAvailable}/${catalogEntries.length}`,
      `${catalogDownloadable} downloadable presets in Image Library`,
      catalogEntries.length > 0 ? "ok" : "warn"
    ),
    renderModuleSyncCard(
      "Node Profiles",
      String(profiles.length),
      `${tailscaleProfiles} profiles have Tailscale enabled`,
      profiles.length > 0 ? "ok" : "warn"
    ),
    renderModuleSyncCard(
      "Discovered Devices",
      String(devices.length),
      deviceErrors > 0 ? `${deviceErrors} ingest errors reported` : "No ingest errors reported",
      deviceErrors > 0 ? "bad" : "ok"
    ),
    renderModuleSyncCard(
      "Callback Events",
      String(events.length),
      `${eventsWithProfile} events include profile metadata`,
      events.length > 0 ? "ok" : "warn"
    ),
    renderModuleSyncCard(
      "Rack Identity",
      rackName,
      "Used by ACL defaults and rack-aware presets",
      rackName ? "ok" : "warn"
    ),
  ];

  moduleSyncGrid.innerHTML = cards.join("");

  if (failed === 0) {
    const now = new Date();
    moduleSyncStatus.textContent = `Snapshot refreshed at ${now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}.`;
  }
}

async function saveRackSettings() {
  const rackName = rackNameInput.value.trim();
  if (!rackName) {
    rackStatus.textContent = "Rack name cannot be empty.";
    return;
  }

  rackStatus.textContent = "Saving rack name...";
  const res = await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ rack_name: rackName }),
  });
  const data = await res.json();
  if (!res.ok || !data.ok) {
    rackStatus.textContent = data.error || "Failed to save rack name.";
    return;
  }
  rackNameInput.value = (data.settings || {}).rack_name || rackName;
  rackStatus.textContent = `Saved rack name: ${rackNameInput.value}`;
}

async function checkCatalogHealth() {
  catalogHealthStatus.textContent = "Checking catalog...";
  const res = await fetch("/api/sd/catalog");
  const data = await res.json();
  if (!res.ok || !data.ok) {
    catalogHealthStatus.textContent = data.error || "Failed to check catalog state.";
    return;
  }

  const entries = Array.isArray(data.entries) ? data.entries : [];
  const downloadable = entries.filter((entry) => String(entry.image_url || "").trim());
  const available = entries.filter((entry) => Boolean(entry.local_available));
  const missingUrl = entries.filter((entry) => !String(entry.image_url || "").trim());
  const sampleDownloadable = downloadable.slice(0, 5).map((entry) => entry.id).join(", ") || "(none)";

  catalogHealthStatus.textContent = [
    `Total entries: ${entries.length}`,
    `Downloadable entries: ${downloadable.length}`,
    `Available in cache/history: ${available.length}`,
    `Manual URL required: ${missingUrl.length}`,
    `Sample downloadable IDs: ${sampleDownloadable}`,
  ].join("\n");
}

async function toggleVerboseLogging(enable) {
  const res = await fetch("/api/logging/verbose", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enable }),
  });
  const data = await res.json();
  updateVerboseButtons(Boolean(data.verbose_logging));
  loggingStatus.textContent = data.message || (data.ok ? "Updated." : "Error.");
}

async function viewRecentLogs() {
  const res = await fetch("/api/logging/tail?lines=100");
  const data = await res.json();
  if (!data.ok) {
    loggingStatus.textContent = "Failed to load logs.";
    return;
  }
  loggingOutput.textContent = data.lines.join("");
  loggingOutput.style.display = "block";
  loggingStatus.textContent = `Loaded from: ${data.log_file}`;
}

function parseStaleHours() {
  const raw = String(staleMaxAgeHoursInput && staleMaxAgeHoursInput.value ? staleMaxAgeHoursInput.value : "168").trim();
  const parsed = Number.parseInt(raw, 10);
  if (!Number.isFinite(parsed) || parsed < 1 || parsed > 8760) {
    return null;
  }
  return parsed;
}

function formatPruneSummary(result) {
  const removed = result && typeof result === "object" ? (result.removed || {}) : {};
  const staleNodes = Array.isArray(result && result.stale_nodes) ? result.stale_nodes : [];
  const nodeNames = staleNodes.slice(0, 12).map((item) => item.name).filter(Boolean).join(", ");
  return [
    `Stale nodes: ${staleNodes.length}`,
    `Callbacks removed: ${removed.callbacks || 0}`,
    `Managed nodes removed: ${removed.managed_nodes || 0}`,
    `Node cloud-init links removed: ${removed.node_cloud_init || 0}`,
    `Node configs removed: ${removed.node_configs || 0}`,
    `Node inputs removed: ${removed.node_inputs || 0}`,
    `Node results removed: ${removed.node_results || 0}`,
    `Manual nodes removed: ${removed.manual_nodes || 0}`,
    nodeNames ? `Sample nodes: ${nodeNames}` : "",
  ].filter(Boolean).join("\n");
}

async function runStalePrune(dryRun) {
  if (!stalePruneStatus) {
    return;
  }
  const hours = parseStaleHours();
  if (hours == null) {
    stalePruneStatus.textContent = "Stale threshold must be an integer between 1 and 8760 hours.";
    return;
  }

  if (!dryRun) {
    const confirmed = window.confirm(`Prune nodes with last callback older than ${hours} hour(s)? This also removes their saved monitor and node ops state.`);
    if (!confirmed) {
      return;
    }
  }

  stalePruneStatus.textContent = dryRun ? "Scanning stale nodes..." : "Pruning stale nodes...";
  try {
    const res = await fetch("/api/nodes/stale/prune", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ max_age_hours: hours, dry_run: dryRun }),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      stalePruneStatus.textContent = data.error || "Stale node operation failed.";
      return;
    }

    const modeText = dryRun ? "Preview" : "Prune complete";
    stalePruneStatus.textContent = `${modeText} (cutoff ${data.cutoff}):\n${formatPruneSummary(data)}`;
  } catch (err) {
    stalePruneStatus.textContent = `Stale node operation failed: ${String(err && err.message ? err.message : err)}`;
  }
}

resetNodeOpsBtn.addEventListener("click", resetNodeOps);
clearCallbackBtn.addEventListener("click", clearCallbackBacklog);
saveRackNameBtn.addEventListener("click", saveRackSettings);
checkCatalogHealthBtn.addEventListener("click", checkCatalogHealth);
enableVerboseBtn.addEventListener("click", () => toggleVerboseLogging(true));
disableVerboseBtn.addEventListener("click", () => toggleVerboseLogging(false));
document.getElementById("view-recent-logs").addEventListener("click", viewRecentLogs);
if (refreshModuleSyncBtn) {
  refreshModuleSyncBtn.addEventListener("click", refreshModuleSync);
}
if (previewStalePruneBtn) {
  previewStalePruneBtn.addEventListener("click", () => runStalePrune(true));
}
if (runStalePruneBtn) {
  runStalePruneBtn.addEventListener("click", () => runStalePrune(false));
}

initSidebar();
loadRackSettings();
refreshModuleSync();
