const bootLogHostEl = document.getElementById("boot-log-host");
const bootLogRouteEl = document.getElementById("boot-log-route");
const bootLogSelectEl = document.getElementById("boot-log-select");
const bootLogStatusEl = document.getElementById("boot-log-status");
const bootLogRouteNoteEl = document.getElementById("boot-log-route-note");
const bootLogViewEl = document.getElementById("boot-log-view");

let archivedBootLogHosts = new Set();
let bootLogRoutes = [];
let bootLogEntries = [];
let bootLogSelection = 0;
let bootLogReadSelection = 0;

async function bootLogJson(url, options) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok || data.ok === false) throw new Error(data.error || "Boot log request failed.");
  return data;
}

function bootLogLabel(entry) {
  const when = formatTimestamp(entry.fetched_at);
  if (entry.status !== "ok") return `${when} - failed (${entry.trigger})`;
  const kb = Math.max(1, Math.round(Number(entry.bytes || 0) / 1024));
  return `${when} - ${kb} KB (${entry.trigger})${entry.truncated ? " truncated" : ""}`;
}

async function showBootLog(host, entry, selection) {
  const read = ++bootLogReadSelection;
  if (!entry) {
    bootLogViewEl.textContent = "No boot log selected.";
    return;
  }
  if (entry.status !== "ok") {
    bootLogViewEl.textContent = `Fetch failed${entry.target ? ` for ${entry.target}` : ""}: ${entry.error || "unknown error"}`;
    return;
  }
  bootLogViewEl.textContent = "Loading stored log...";
  const data = await bootLogJson(`/api/nodes/${encodeURIComponent(host)}/boot-logs/${encodeURIComponent(entry.id)}`);
  if (selection !== bootLogSelection || read !== bootLogReadSelection) return;
  bootLogViewEl.textContent = data.text;
}

async function loadBootLogList(host, selection, selectId) {
  const data = await bootLogJson(`/api/nodes/${encodeURIComponent(host)}/boot-logs`);
  if (selection !== bootLogSelection) return;
  if (!Array.isArray(data.logs)) throw new Error("Invalid stored-log response.");
  bootLogEntries = data.logs;
  bootLogSelectEl.replaceChildren();
  for (const entry of bootLogEntries) {
    bootLogSelectEl.add(new Option(bootLogLabel(entry), entry.id));
  }
  if (selectId && bootLogEntries.some(entry => entry.id === selectId)) bootLogSelectEl.value = selectId;
  bootLogStatusEl.textContent = bootLogEntries.length
    ? `${bootLogEntries.length} stored log(s) for ${host} (newest first).`
    : `No boot logs stored for ${host} yet.`;
  await showBootLog(host, bootLogEntries.find(entry => entry.id === bootLogSelectEl.value), selection);
}

function describeBootLogRoute() {
  const profile = bootLogRoutes.find(item => item.id === bootLogRouteEl.value);
  if (!profile) return;
  const route = profile.route || {};
  bootLogRouteNoteEl.textContent = [
    `${profile.kind}: ${profile.user ? `${profile.user}@` : ""}${profile.address}:${profile.port}`,
    route.interface ? `container interface ${route.interface}` : "",
    route.gateway ? `gateway ${route.gateway}` : "",
    route.source_ip ? `source ${route.source_ip}` : "",
    route.error || "",
    "Candidate only; SSH collection tests reachability.",
  ].filter(Boolean).join(" · ");
}

async function collectSelectedBootLogs(selection) {
  const host = bootLogHostEl.value;
  const routeId = bootLogRouteEl.value;
  if (!host || !routeId || archivedBootLogHosts.has(host)) return;
  describeBootLogRoute();
  bootLogStatusEl.textContent = `Collecting boot logs from ${host} via ${routeId} (up to 90 seconds)...`;
  try {
    const response = await fetch(`/api/nodes/${encodeURIComponent(host)}/boot-logs/fetch`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({route_id: routeId}),
    });
    const data = await response.json();
    if (selection !== bootLogSelection) return;
    await loadBootLogList(host, selection, data.log && data.log.id);
    if (selection !== bootLogSelection) return;
    if (!response.ok || !data.ok) throw new Error(data.error || (data.log && data.log.error) || "Log collection failed.");
    bootLogStatusEl.textContent = `Collected fresh logs via ${data.log.target}. ${bootLogEntries.length} stored log(s).`;
  } catch (error) {
    if (selection === bootLogSelection) bootLogStatusEl.textContent = `Could not collect logs: ${error.message}. Stored history remains available.`;
  }
}

async function selectBootLogHost() {
  const selection = ++bootLogSelection;
  ++bootLogReadSelection;
  const host = bootLogHostEl.value;
  bootLogRoutes = [];
  bootLogEntries = [];
  bootLogSelectEl.replaceChildren();
  bootLogRouteEl.replaceChildren(new Option("Select a node first...", ""));
  bootLogRouteEl.disabled = true;
  bootLogViewEl.textContent = "No boot log selected.";
  bootLogRouteNoteEl.textContent = "Routes originate inside the dashboard container, not the remote node.";
  if (!host) {
    bootLogStatusEl.textContent = "Pick a node to see its stored boot logs.";
    return;
  }
  bootLogStatusEl.textContent = `Loading stored logs and routes for ${host}...`;
  try {
    await loadBootLogList(host, selection);
  } catch (error) {
    if (selection === bootLogSelection) bootLogStatusEl.textContent = `Could not load stored logs: ${error.message}`;
  }
  if (selection !== bootLogSelection) return;
  if (archivedBootLogHosts.has(host)) {
    bootLogRouteEl.replaceChildren(new Option("Archived - stored logs only", ""));
    bootLogRouteNoteEl.textContent = "Restore this node in Settings before collecting new logs.";
    return;
  }
  try {
    const data = await bootLogJson(`/api/nodes/${encodeURIComponent(host)}/connection-profiles?routes=1`);
    if (selection !== bootLogSelection) return;
    if (!Array.isArray(data.candidates)) throw new Error("Invalid connection-route response.");
    bootLogRoutes = data.candidates;
    bootLogRouteEl.replaceChildren(new Option("Select a route...", ""));
    for (const profile of bootLogRoutes) {
      const iface = profile.route && profile.route.interface;
      bootLogRouteEl.add(new Option(`${profile.kind} - ${profile.address}:${profile.port}${iface ? ` via ${iface}` : ""}${profile.id === data.selected ? " (preferred)" : ""}`, profile.id));
    }
    bootLogRouteEl.disabled = !bootLogRoutes.length;
    bootLogRouteEl.value = data.selected || "";
    if (!bootLogRouteEl.value) {
      bootLogStatusEl.textContent = "No preferred route available. Select a route to collect logs.";
      return;
    }
    await collectSelectedBootLogs(selection);
  } catch (error) {
    if (selection === bootLogSelection) bootLogStatusEl.textContent = `Could not load routes: ${error.message}. Stored history remains available.`;
  }
}

bootLogHostEl.addEventListener("change", selectBootLogHost);
bootLogRouteEl.addEventListener("change", () => collectSelectedBootLogs(++bootLogSelection));
bootLogSelectEl.addEventListener("change", () => {
  const selection = bootLogSelection;
  showBootLog(bootLogHostEl.value, bootLogEntries.find(entry => entry.id === bootLogSelectEl.value), selection)
    .catch(error => {
      if (selection === bootLogSelection) bootLogStatusEl.textContent = `Could not read stored log: ${error.message}`;
    });
});

async function loadBootLogHosts() {
  try {
    const data = await bootLogJson("/api/nodes/boot-log-hosts");
    const current = bootLogHostEl.value;
    const wasArchived = archivedBootLogHosts.has(current);
    archivedBootLogHosts = new Set(data.hosts.filter(item => item.archived).map(item => item.name));
    bootLogHostEl.replaceChildren(new Option("Select a node...", ""));
    for (const host of data.hosts) {
      bootLogHostEl.add(new Option(host.name + (host.archived ? " (archived - stored logs only)" : ""), host.name));
    }
    if (data.hosts.some(item => item.name === current)) bootLogHostEl.value = current;
    if (current && (bootLogHostEl.value !== current || wasArchived !== archivedBootLogHosts.has(current))) {
      await selectBootLogHost();
    }
  } catch (error) {
    bootLogStatusEl.textContent = `Could not load log hosts: ${error.message}`;
  }
}
