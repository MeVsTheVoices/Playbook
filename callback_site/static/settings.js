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

resetNodeOpsBtn.addEventListener("click", resetNodeOps);
clearCallbackBtn.addEventListener("click", clearCallbackBacklog);
saveRackNameBtn.addEventListener("click", saveRackSettings);
checkCatalogHealthBtn.addEventListener("click", checkCatalogHealth);
enableVerboseBtn.addEventListener("click", () => toggleVerboseLogging(true));
disableVerboseBtn.addEventListener("click", () => toggleVerboseLogging(false));
document.getElementById("view-recent-logs").addEventListener("click", viewRecentLogs);

initSidebar();
loadRackSettings();
