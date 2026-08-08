const body = document.body;
const toggleBtn = document.getElementById("sidebar-toggle");
const hostEl = document.getElementById("host");
const hostSelectEl = document.getElementById("host-select");
const useSelectedHostBtn = document.getElementById("use-selected-host");
const refreshTargetsBtn = document.getElementById("refresh-targets");
const saveManualHostBtn = document.getElementById("save-manual-host");
const usernameEl = document.getElementById("username");
const portEl = document.getElementById("port");
const keyNameEl = document.getElementById("key-name");
const checkIdEl = document.getElementById("check-id");
const checkDescriptionEl = document.getElementById("check-description");
const startBtn = document.getElementById("start-check");
const refreshKeysBtn = document.getElementById("refresh-keys");
const jobStatusEl = document.getElementById("job-status");
const dnsHintEl = document.getElementById("dns-hint");
const jobOutputEl = document.getElementById("job-output");

let activeJobId = null;
let pollTimer = null;
let checksCatalog = [];
let callbackTargets = [];
const MANUAL_HOSTS_KEY = "verifier-manual-hosts";

function initSidebar() {
  const saved = localStorage.getItem("sidebar-collapsed");
  if (saved === "1") {
    body.classList.add("sidebar-collapsed");
  }
}

function statusChip(status) {
  const s = String(status || "unknown").toLowerCase();
  let cls = "chip";
  if (s === "running") cls += " running";
  if (s === "completed") cls += " completed";
  if (s === "failed") cls += " failed";
  return `<span class="${cls}">${s}</span>`;
}

function resolveVerificationHost() {
  return hostSelectEl.value.trim() || hostEl.value.trim();
}

function renderJob(job) {
  const started = job?.started_at || "-";
  const finished = job?.finished_at || "-";
  const code = job?.exit_code;
  const codeText = code === null || code === undefined ? "pending" : String(code);
  jobStatusEl.innerHTML = [
    `Job: ${job?.id || activeJobId || "-"}`,
    `Status: ${statusChip(job?.status)}`,
    `Started: ${started}`,
    `Finished: ${finished}`,
    `Exit code: ${codeText}`,
  ].join("\n");
  const out = `${job?.stdout || ""}${job?.stderr ? "\n[stderr]\n" + job.stderr : ""}`.trim();
  jobOutputEl.textContent = out || "No output yet.";

  const dnsError = String(job?.stderr || "").includes("Could not resolve hostname");
  if (dnsError) {
    const hostValue = hostEl.value.trim() || hostSelectEl.value.trim();
    const ipTarget = callbackTargets.find((t) => {
      if (String(t.source || "") !== "source_ip") {
        return false;
      }
      const hostname = String(t.hostname || "").trim();
      const fqdn = String(t.fqdn || "").trim();
      return hostname === hostValue || fqdn === hostValue;
    });

    if (ipTarget && ipTarget.host) {
      dnsHintEl.innerHTML = `DNS lookup failed for ${hostValue}. Try callback source IP ${ipTarget.host} instead. <button class="mini-btn" id="use-ip-fallback" type="button">Use ${ipTarget.host}</button>`;
      dnsHintEl.style.display = "block";
      const useIpBtn = document.getElementById("use-ip-fallback");
      if (useIpBtn) {
        useIpBtn.addEventListener("click", () => {
          hostEl.value = String(ipTarget.host || "");
          hostSelectEl.value = String(ipTarget.host || "");
          jobStatusEl.textContent = `Switched host to callback IP fallback '${ipTarget.host}'.`;
        });
      }
    } else {
      dnsHintEl.textContent = `DNS lookup failed for ${hostValue}. Consider using the callback source IP from discovered hosts.`;
      dnsHintEl.style.display = "block";
    }
  } else {
    dnsHintEl.style.display = "none";
    dnsHintEl.textContent = "";
  }
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function pollJob() {
  if (!activeJobId) {
    return;
  }
  try {
    const res = await fetch(`/api/test-terminal/jobs/${encodeURIComponent(activeJobId)}`);
    const payload = await res.json();
    if (!res.ok || !payload.ok || !payload.job) {
      throw new Error(payload.error || "job not found");
    }
    const job = payload.job;
    renderJob(job);
    if (job.status === "completed" || job.status === "failed") {
      stopPolling();
    }
  } catch (err) {
    jobStatusEl.textContent = `Polling error: ${String(err.message || err)}`;
    stopPolling();
  }
}

async function startCheck() {
  const host = resolveVerificationHost();
  if (!host) {
    jobStatusEl.textContent = "Host is required.";
    hostEl.focus();
    return;
  }
  if (!checkIdEl.value) {
    jobStatusEl.textContent = "Select a verification check.";
    checkIdEl.focus();
    return;
  }

  stopPolling();
  activeJobId = null;
  jobStatusEl.textContent = "Starting check...";
  jobOutputEl.textContent = "Launching remote command...";

  try {
    const res = await fetch("/api/test-terminal/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        host,
        username: usernameEl.value.trim() || "jdizzle",
        key_name: keyNameEl.value,
        check_id: checkIdEl.value,
        port: Number(portEl.value || "22"),
      }),
    });
    const payload = await res.json();
    if (!res.ok || !payload.ok || !payload.job_id) {
      throw new Error(payload.error || "start failed");
    }
    activeJobId = payload.job_id;
    await pollJob();
    pollTimer = setInterval(pollJob, 1200);
  } catch (err) {
    jobStatusEl.textContent = `Start error: ${String(err.message || err)}`;
    jobOutputEl.textContent = "No output yet.";
  }
}

async function refreshKeys() {
  try {
    const res = await fetch("/api/ssh/keys");
    const payload = await res.json();
    if (!res.ok || !payload.ok) {
      throw new Error(payload.error || "failed to load keys");
    }
    const keys = Array.isArray(payload.keys) ? payload.keys : [];
    keyNameEl.innerHTML = "";
    if (!keys.length) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "No keys found";
      keyNameEl.appendChild(opt);
      return;
    }
    for (const key of keys) {
      const keyName = String(key.key_name || key.name || "").trim();
      if (!keyName) {
        continue;
      }
      const opt = document.createElement("option");
      opt.value = keyName;
      const pubState = key.public_key_exists ? "pub" : "no-pub";
      opt.textContent = `${keyName} (${pubState})`;
      keyNameEl.appendChild(opt);
    }
    const preferred = keys.find((k) => String(k.key_name || k.name || "") === "rack-bootstrap-ed25519");
    if (preferred) {
      keyNameEl.value = "rack-bootstrap-ed25519";
    } else if (keyNameEl.options.length) {
      keyNameEl.value = keyNameEl.options[0].value;
    }
    jobStatusEl.textContent = `Loaded ${keys.length} SSH key(s) from callback key store.`;
  } catch (err) {
    jobStatusEl.textContent = `Key refresh error: ${String(err.message || err)}`;
  }
}

function readManualHosts() {
  try {
    const raw = JSON.parse(localStorage.getItem(MANUAL_HOSTS_KEY) || "[]");
    if (!Array.isArray(raw)) {
      return [];
    }
    return raw
      .map((v) => String(v || "").trim())
      .filter((v) => Boolean(v));
  } catch (err) {
    return [];
  }
}

function writeManualHosts(values) {
  const normalized = values
    .map((v) => String(v || "").trim())
    .filter((v) => Boolean(v));
  localStorage.setItem(MANUAL_HOSTS_KEY, JSON.stringify(Array.from(new Set(normalized)).slice(0, 40)));
}

function renderTargets() {
  const current = hostSelectEl.value;
  const manual = readManualHosts();
  hostSelectEl.innerHTML = "";

  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = "Select discovered host...";
  hostSelectEl.appendChild(placeholder);

  for (const target of callbackTargets) {
    const opt = document.createElement("option");
    opt.value = target.host;
    const profile = target.profile ? ` | ${target.profile}` : "";
    const assoc = target.hostname || target.fqdn || "unmapped";
    if (target.source === "source_ip") {
      const quality = target.usable_callback_ip === false ? "transit" : "node-ip";
      opt.textContent = `${target.host} (ip for ${assoc} | ${quality}${profile})`;
    } else {
      opt.textContent = `${target.host} (${target.source}${profile})`;
    }
    hostSelectEl.appendChild(opt);
  }

  const discovered = new Set(callbackTargets.map((t) => String(t.host || "").toLowerCase()));
  for (const host of manual) {
    if (discovered.has(host.toLowerCase())) {
      continue;
    }
    const opt = document.createElement("option");
    opt.value = host;
    opt.textContent = `${host} (manual)`;
    hostSelectEl.appendChild(opt);
  }

  if (current && Array.from(hostSelectEl.options).some((o) => o.value === current)) {
    hostSelectEl.value = current;
  }
}

async function refreshTargets() {
  try {
    const res = await fetch("/api/test-terminal/targets");
    const payload = await res.json();
    if (!res.ok || !payload.ok) {
      throw new Error(payload.error || "failed to load targets");
    }
    callbackTargets = Array.isArray(payload.targets) ? payload.targets : [];
    renderTargets();
  } catch (err) {
    jobStatusEl.textContent = `Host refresh error: ${String(err.message || err)}`;
    renderTargets();
  }
}

function saveManualHost() {
  const value = hostEl.value.trim();
  if (!value) {
    jobStatusEl.textContent = "Enter a host first, then save it.";
    return;
  }
  const existing = readManualHosts();
  existing.push(value);
  writeManualHosts(existing);
  renderTargets();
  hostSelectEl.value = value;
  jobStatusEl.textContent = `Saved manual host '${value}'.`;
}

function useSelectedHost() {
  const selected = hostSelectEl.value.trim();
  if (!selected) {
    jobStatusEl.textContent = "Select a discovered host first.";
    return;
  }
  hostEl.value = selected;
  jobStatusEl.textContent = `Using host '${selected}'.`;
}

function renderChecks() {
  checkIdEl.innerHTML = "";
  for (const item of checksCatalog) {
    const opt = document.createElement("option");
    opt.value = item.id;
    opt.textContent = item.label;
    checkIdEl.appendChild(opt);
  }
  if (!checkIdEl.value && checksCatalog.length) {
    checkIdEl.value = checksCatalog[0].id;
  }
  updateCheckDescription();
}

function updateCheckDescription() {
  const selected = checksCatalog.find((c) => c.id === checkIdEl.value);
  checkDescriptionEl.textContent = selected?.description || "Choose a predefined check.";
}

async function refreshChecks() {
  try {
    const res = await fetch("/api/test-terminal/checks");
    const payload = await res.json();
    if (!res.ok || !payload.ok) {
      throw new Error(payload.error || "failed to load checks");
    }
    checksCatalog = Array.isArray(payload.checks) ? payload.checks : [];
    renderChecks();
  } catch (err) {
    jobStatusEl.textContent = `Checks refresh error: ${String(err.message || err)}`;
  }
}

toggleBtn.addEventListener("click", () => {
  body.classList.toggle("sidebar-collapsed");
  localStorage.setItem("sidebar-collapsed", body.classList.contains("sidebar-collapsed") ? "1" : "0");
});

startBtn.addEventListener("click", startCheck);
refreshKeysBtn.addEventListener("click", refreshKeys);
checkIdEl.addEventListener("change", updateCheckDescription);
useSelectedHostBtn.addEventListener("click", useSelectedHost);
refreshTargetsBtn.addEventListener("click", refreshTargets);
saveManualHostBtn.addEventListener("click", saveManualHost);
hostSelectEl.addEventListener("change", useSelectedHost);
initSidebar();
refreshKeys();
refreshChecks();
renderTargets();
refreshTargets();
