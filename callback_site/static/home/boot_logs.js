const bootLogHostEl = document.getElementById("boot-log-host");

const bootLogSelectEl = document.getElementById("boot-log-select");

const bootLogFetchBtn = document.getElementById("boot-log-fetch");

const bootLogStatusEl = document.getElementById("boot-log-status");

const bootLogViewEl = document.getElementById("boot-log-view");

let bootLogHostsKey = "";

function refreshBootLogHosts(events) {
  const hosts = [...new Set((events || []).map((e) => String(e.hostname || "").trim()).filter(Boolean))].sort();
  const key = hosts.join("|");
  if (key === bootLogHostsKey) return;
  bootLogHostsKey = key;
  const current = bootLogHostEl.value;
  bootLogHostEl.innerHTML = "<option value=''>Select a node...</option>";
  for (const host of hosts) {
    const option = document.createElement("option");
    option.value = host;
    option.textContent = host;
    bootLogHostEl.appendChild(option);
  }
  if (current && hosts.includes(current)) {
    bootLogHostEl.value = current;
  }
}

function bootLogLabel(entry) {
  const when = formatEventTimestamp(entry.fetched_at);
  if (entry.status !== "ok") {
    return `${when} - failed (${entry.trigger})`;
  }
  const kb = Math.max(1, Math.round(Number(entry.bytes || 0) / 1024));
  return `${when} - ${kb} KB (${entry.trigger})${entry.truncated ? " truncated" : ""}`;
}

async function showBootLog(host, entry) {
  if (!entry) {
    bootLogViewEl.textContent = "No boot log selected.";
    return;
  }
  if (entry.status !== "ok") {
    bootLogViewEl.textContent = `Fetch failed${entry.target ? ` for ${entry.target}` : ""}: ${entry.error || "unknown error"}`;
    return;
  }
  bootLogViewEl.textContent = "Loading...";
  const res = await fetch(`/api/nodes/${encodeURIComponent(host)}/boot-logs/${encodeURIComponent(entry.id)}`);
  const data = await res.json();
  bootLogViewEl.textContent = res.ok && data.ok ? data.text : (data.error || "Could not load that log.");
}

async function loadBootLogList(selectId) {
  const host = bootLogHostEl.value;
  bootLogSelectEl.innerHTML = "";
  if (!host) {
    bootLogStatusEl.textContent = "Pick a node to see its stored boot logs. They are pulled over SSH shortly after a node calls back.";
    bootLogViewEl.textContent = "No boot log selected.";
    bootLogFetchBtn.disabled = true;
    return;
  }
  bootLogFetchBtn.disabled = false;
  const res = await fetch(`/api/nodes/${encodeURIComponent(host)}/boot-logs`);
  const data = await res.json();
  const logs = res.ok && Array.isArray(data.logs) ? data.logs : [];
  for (const entry of logs) {
    const option = document.createElement("option");
    option.value = entry.id;
    option.textContent = bootLogLabel(entry);
    bootLogSelectEl.appendChild(option);
  }
  if (!logs.length) {
    bootLogStatusEl.textContent = `No boot logs stored for ${host} yet.`;
    bootLogViewEl.textContent = "No boot log selected.";
    return;
  }
  bootLogStatusEl.textContent = `${logs.length} stored boot log${logs.length === 1 ? "" : "s"} for ${host} (newest first).`;
  if (selectId && logs.some((entry) => entry.id === selectId)) {
    bootLogSelectEl.value = selectId;
  }
  await showBootLog(host, logs.find((entry) => entry.id === bootLogSelectEl.value));
}

async function fetchBootLogsNow() {
  const host = bootLogHostEl.value;
  if (!host) return;
  bootLogFetchBtn.disabled = true;
  bootLogStatusEl.textContent = `Collecting boot logs from ${host} over SSH (can take up to a minute)...`;
  try {
    const res = await fetch(`/api/nodes/${encodeURIComponent(host)}/boot-logs/fetch`, { method: "POST" });
    const data = await res.json();
    await loadBootLogList((data.log || {}).id);
    if (!data.ok) {
      bootLogStatusEl.textContent = `Fetch failed: ${(data.log || {}).error || data.error || "unknown error"}`;
    }
  } catch (err) {
    bootLogStatusEl.textContent = `Fetch error: ${String(err.message || err)}`;
  } finally {
    bootLogFetchBtn.disabled = !bootLogHostEl.value;
  }
}

bootLogHostEl.addEventListener("change", () => {
  if (bootLogHostEl.value) setTerminalHost(bootLogHostEl.value);
  loadBootLogList();
});

bootLogSelectEl.addEventListener("change", async () => {
  const host = bootLogHostEl.value;
  const res = await fetch(`/api/nodes/${encodeURIComponent(host)}/boot-logs`);
  const data = await res.json();
  const logs = res.ok && Array.isArray(data.logs) ? data.logs : [];
  await showBootLog(host, logs.find((entry) => entry.id === bootLogSelectEl.value));
});

bootLogFetchBtn.addEventListener("click", fetchBootLogsNow);
