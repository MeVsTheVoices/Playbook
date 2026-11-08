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
const refreshDataPipelinesBtn = document.getElementById("refresh-data-pipelines");
const dataPipelinesStatus = document.getElementById("data-pipelines-status");
const dataPipelinesGrid = document.getElementById("data-pipelines-grid");
const archiveStatus = document.getElementById("archive-status");

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

async function fetchJsonSnapshot(url) {
  try {
    const res = await fetch(url);
    const data = await res.json();
    return { ok: res.ok && Boolean(data), status: res.status, data };
  } catch (err) {
    return { ok: false, status: 0, data: { error: String(err && err.message ? err.message : err) } };
  }
}

function countPowerRestoreEvents(events) {
  return events.filter((event) => String(event.stage || "").trim().toLowerCase() === "power_restore").length;
}

function countUuidLikeIds(events) {
  const uuidLike = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
  return events.filter((event) => uuidLike.test(String(event.instance_id || "").trim())).length;
}

async function refreshDataPipelinesSnapshot() {
  if (!refreshDataPipelinesBtn || !dataPipelinesStatus || !dataPipelinesGrid) {
    return;
  }

  dataPipelinesStatus.textContent = "Refreshing data/pull snapshot...";

  const [sotResp, imagesResp, catalogResp, profilesResp, eventsResp, k8sResp] = await Promise.all([
    fetchJsonSnapshot("/api/state/source-of-truth"),
    fetchJsonSnapshot("/api/sd/images"),
    fetchJsonSnapshot("/api/sd/image-catalog"),
    fetchJsonSnapshot("/api/sd/options-profiles"),
    fetchJsonSnapshot("/api/cloud-init/events"),
    fetchJsonSnapshot("/api/kubernetes/summary"),
  ]);

  const sourceState = sotResp.ok && sotResp.data && sotResp.data.state ? sotResp.data.state : {};
  const sourceCounts = sourceState.counts || {};
  const sourceMonitor = sourceState.monitor || {};
  const sourcePaths = sourceState.sources || {};

  const imageEntries = imagesResp.ok && Array.isArray(imagesResp.data && imagesResp.data.entries)
    ? imagesResp.data.entries
    : [];
  const catalogEntries = catalogResp.ok && Array.isArray(catalogResp.data && catalogResp.data.entries)
    ? catalogResp.data.entries
    : [];
  const optionProfiles = profilesResp.ok && Array.isArray(profilesResp.data && profilesResp.data.profiles)
    ? profilesResp.data.profiles
    : [];
  const callbackEvents = eventsResp.ok && Array.isArray(eventsResp.data && eventsResp.data.events)
    ? eventsResp.data.events
    : [];
  const k8sSummary = k8sResp.ok && k8sResp.data ? k8sResp.data : {};

  const powerRestoreEvents = countPowerRestoreEvents(callbackEvents);
  const certEvents = callbackEvents.filter((event) => Boolean(event.cert_present)).length;
  const nodeReportEvents = callbackEvents.filter((event) => Boolean(event.node_report_present)).length;
  const uuidLikeEvents = countUuidLikeIds(callbackEvents);
  const catalogPullRecords = imageEntries.filter((entry) => String(entry.source || "").trim() === "catalog_pull").length;
  const assignedNodeCloudInit = Object.keys(sourceMonitor.node_cloud_init || {}).length;
  const cloudInitTemplateCount = Array.isArray(sourceMonitor.cloud_init_options) ? sourceMonitor.cloud_init_options.length : 0;
  const availableCatalog = Number(sourceCounts.catalog_available || 0);
  const totalCatalog = Number(sourceCounts.catalog_entries || catalogEntries.length || 0);

  const cards = [
    renderModuleSyncCard(
      "Source Of Truth",
      `${Number(sourceCounts.nodes_in_monitor || 0)}/${Number(sourceCounts.nodes_in_node_ops || 0)}`,
      "monitor/node-ops node counts in unified state",
      sotResp.ok ? "ok" : "warn"
    ),
    renderModuleSyncCard(
      "Callback Signals",
      `${callbackEvents.length}`,
      `${powerRestoreEvents} power_restore, ${certEvents} cert, ${nodeReportEvents} node-report`,
      callbackEvents.length > 0 ? "ok" : "warn"
    ),
    renderModuleSyncCard(
      "Identity Signals",
      `${uuidLikeEvents}`,
      "callbacks with UUID-like instance IDs",
      uuidLikeEvents > 0 ? "ok" : "warn"
    ),
    renderModuleSyncCard(
      "Cloud-Init Templates",
      String(optionProfiles.length),
      `${cloudInitTemplateCount} option defs, ${assignedNodeCloudInit} node assignments`,
      optionProfiles.length > 0 ? "ok" : "warn"
    ),
    renderModuleSyncCard(
      "Image Pull Records",
      String(imageEntries.length),
      `${catalogPullRecords} records from catalog-pull jobs`,
      imageEntries.length > 0 ? "ok" : "warn"
    ),
    renderModuleSyncCard(
      "Catalog Cache",
      `${availableCatalog}/${totalCatalog}`,
      "catalog entries marked locally available",
      availableCatalog > 0 ? "ok" : "warn"
    ),
    renderModuleSyncCard(
      "Kubernetes Jobs",
      `${Number(k8sSummary.jobs_running || 0)}/${Number(k8sSummary.jobs_total || 0)}`,
      k8sSummary.available ? "running/total jobs from local summary" : "runtime unavailable on this host",
      k8sSummary.available ? "ok" : "warn"
    ),
    renderModuleSyncCard(
      "Dataset Paths",
      String(Object.keys(sourcePaths).length),
      Object.keys(sourcePaths).length ? "callbacks, monitor, provisioning, catalog, inventory" : "source paths unavailable",
      Object.keys(sourcePaths).length ? "ok" : "warn"
    ),
  ];

  dataPipelinesGrid.innerHTML = cards.join("");

  const failed = [sotResp, imagesResp, catalogResp, profilesResp, eventsResp].filter((resp) => !resp.ok).length;
  const now = new Date();
  if (failed > 0) {
    dataPipelinesStatus.textContent = `Snapshot refreshed with ${failed} endpoint warning${failed === 1 ? "" : "s"} at ${now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}.`;
  } else {
    dataPipelinesStatus.textContent = `Snapshot refreshed at ${now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}.`;
  }
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

async function refreshArchivedNodes() {
  archiveStatus.textContent = "Loading archived nodes...";
  try {
    const res = await fetch("/api/nodes/archive");
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || "Could not load archive.");
    const list = document.getElementById("archived-node-list");
    list.replaceChildren();
    for (const node of data.nodes) {
      const card = document.createElement("article");
      card.className = "module-card";
      const title = document.createElement("h3");
      title.textContent = node.name;
      const note = document.createElement("p");
      note.className = "note";
      note.textContent = `Archived ${new Date(node.archived_at).toLocaleString()} · history retained`;
      const restore = document.createElement("button");
      restore.type = "button";
      restore.className = "primary";
      restore.textContent = "Restore node";
      restore.addEventListener("click", async () => {
        restore.disabled = true;
        try {
          const response = await fetch(`/api/nodes/${encodeURIComponent(node.name)}/archive`, {
            method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify({archived: false}),
          });
          const result = await response.json();
          if (!response.ok || !result.ok) throw new Error(result.error || "Restore failed.");
          await refreshArchivedNodes();
          refreshModuleSync();
        } catch (error) {
          archiveStatus.textContent = `Could not restore node: ${error.message}`;
          restore.disabled = false;
        }
      });
      card.append(title, note, restore);
      list.append(card);
    }
    archiveStatus.textContent = data.nodes.length ? `${data.nodes.length} archived node(s). Restore returns their saved history to active views.` : "No archived nodes. Archive a node from its newest callback card.";
  } catch (err) {
    archiveStatus.textContent = `Could not load archive: ${err.message}`;
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
if (refreshDataPipelinesBtn) {
  refreshDataPipelinesBtn.addEventListener("click", refreshDataPipelinesSnapshot);
}
document.getElementById("refresh-archived-nodes").addEventListener("click", refreshArchivedNodes);

initSidebar();
loadRackSettings();
refreshModuleSync();
refreshDataPipelinesSnapshot();
refreshArchivedNodes();
