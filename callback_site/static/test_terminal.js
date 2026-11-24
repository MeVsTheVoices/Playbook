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
const taskSearchEl = document.getElementById("task-search");
const taskCategoriesEl = document.getElementById("task-categories");
const taskCardsEl = document.getElementById("task-cards");
const routeEl = document.getElementById("terminal-route");
const routesBtn = document.getElementById("terminal-routes");
const parametersEl = document.getElementById("task-parameters");
const kubernetesHintEl = document.getElementById("kubernetes-hint");

let activeJobId = null;
let pollTimer = null;
let checksCatalog = [];
let callbackTargets = [];
let activeCategory = "All tasks";
let jobRunning = false;
let routeRequestId = 0;
let routeTimer = null;
let previewRequestId = 0;
let previewTimer = null;
let previewValid = true;
const parameterValues = {};
const MANUAL_HOSTS_KEY = "terminal-manual-hosts";
const LEGACY_MANUAL_HOSTS_KEY = "verifier-manual-hosts";

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
  return hostEl.value.trim();
}

function updateRunButton() {
  startBtn.disabled = jobRunning || !previewValid || !resolveVerificationHost() || !checkIdEl.value || !keyNameEl.value;
  startBtn.textContent = jobRunning ? "Task running..." : "Run selected task";
}

function currentTarget() {
  const host = resolveVerificationHost().toLowerCase();
  return callbackTargets.find((target) => [target.host, target.connect_host, target.hostname, target.fqdn]
    .some((value) => String(value || "").trim().toLowerCase() === host));
}

function syncHostIdentity() {
  const target = currentTarget();
  if (!target) {
    return;
  }
  const username = String(target.node_username || target.username || "").trim();
  if (username) {
    usernameEl.value = username;
  }
  const port = Number.parseInt(String(target.port || target.ssh_port || ""), 10);
  if (Number.isInteger(port) && port >= 1 && port <= 65535) {
    portEl.value = String(port);
  }
  const certificate = String(target.certificate || "").trim();
  if (certificate && Array.from(keyNameEl.options).some((option) => option.value === certificate)) {
    keyNameEl.value = certificate;
  }
  updateRunButton();
}

async function refreshRoute() {
  const requestId = ++routeRequestId;
  const host = resolveVerificationHost();
  syncHostIdentity();
  routesBtn.disabled = !currentTarget()?.hostname;
  updateRunButton();
  if (!host) {
    routeEl.textContent = "Choose a host to see where your task will connect.";
    return;
  }
  routeEl.textContent = `Looking up the route to ${host}...`;
  try {
    const response = await fetch(`/api/test-terminal/route?host=${encodeURIComponent(host)}`);
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || "Could not load route.");
    if (requestId !== routeRequestId) return;
    routeEl.textContent = `${usernameEl.value.trim() || "jdizzle"}@${data.resolved_host}:${portEl.value || "22"} · ${NodeRoutes.summary(data.route)}`;
  } catch (error) {
    if (requestId === routeRequestId) routeEl.textContent = `Route lookup failed: ${error.message}`;
  }
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
  if (job?.resolved_host) {
    routeRequestId += 1;
    routeEl.textContent = `Task SSH target: ${job.resolved_host} · ${job.check_label || job.check_id} · ${job.status}`;
  }

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
      dnsHintEl.textContent = `DNS lookup failed for ${hostValue}. Try callback source IP ${ipTarget.host} instead. `;
      const useIpBtn = document.createElement("button");
      useIpBtn.className = "mini-btn";
      useIpBtn.type = "button";
      useIpBtn.textContent = `Use ${ipTarget.host}`;
      dnsHintEl.append(useIpBtn);
      dnsHintEl.style.display = "block";
      useIpBtn.addEventListener("click", () => {
        hostEl.value = String(ipTarget.host || "");
        hostSelectEl.value = String(ipTarget.host || "");
        jobStatusEl.textContent = `Switched host to callback IP fallback '${ipTarget.host}'.`;
        refreshRoute();
      });
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
      jobRunning = false;
      updateRunButton();
    }
  } catch (err) {
    jobStatusEl.textContent = `Polling error: ${String(err.message || err)}`;
    stopPolling();
    jobRunning = false;
    updateRunButton();
  }
}

async function startCheck() {
  if (jobRunning) return;
  for (const input of parametersEl.querySelectorAll("input, select")) {
    if (!input.checkValidity()) {
      input.reportValidity();
      return;
    }
  }
  const host = resolveVerificationHost();
  if (!host) {
    jobStatusEl.textContent = "Host is required.";
    hostEl.focus();
    return;
  }
  if (!checkIdEl.value) {
    jobStatusEl.textContent = "Select a terminal task.";
    checkIdEl.focus();
    return;
  }
  if (!portEl.checkValidity()) {
    jobStatusEl.textContent = "Choose an SSH port between 1 and 65535.";
    portEl.reportValidity();
    return;
  }

  stopPolling();
  jobRunning = true;
  updateRunButton();
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
        parameters: readTaskParameters(),
      }),
    });
    const payload = await res.json();
    if (!res.ok || !payload.ok || !payload.job_id) {
      throw new Error(payload.error || "start failed");
    }
    activeJobId = payload.job_id;
    await pollJob();
    if (jobRunning) pollTimer = setInterval(pollJob, 1200);
  } catch (err) {
    jobRunning = false;
    updateRunButton();
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
      updateRunButton();
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
    updateRunButton();
  } catch (err) {
    jobStatusEl.textContent = `Key refresh error: ${String(err.message || err)}`;
  }
}

function readManualHosts() {
  try {
    let rawText = localStorage.getItem(MANUAL_HOSTS_KEY);
    if (!rawText) {
      rawText = localStorage.getItem(LEGACY_MANUAL_HOSTS_KEY) || "[]";
    }
    const raw = JSON.parse(rawText || "[]");
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

  const discoveredGroups = new Map();
  for (const target of callbackTargets) {
    const connectHost = String(target.connect_host || target.host || "").trim();
    if (!connectHost) {
      continue;
    }
    const displayHost = String(target.host || connectHost).trim();
    const groupLabel = String(
      target.hostname ||
      target.fqdn ||
      target.label_hint ||
      displayHost ||
      "Other discovered hosts"
    ).trim();
    if (!discoveredGroups.has(groupLabel)) {
      discoveredGroups.set(groupLabel, new Map());
    }
    const opt = document.createElement("option");
    opt.value = connectHost;
    const profile = target.profile ? ` | ${target.profile}` : "";
    const assoc = target.hostname || target.fqdn || "unmapped";
    const aliasHint = displayHost !== connectHost ? ` as ${displayHost}` : "";
    if (target.source === "source_ip" || target.source === "callback") {
      const quality = target.usable_callback_ip === false ? "transit" : "node-ip";
      opt.textContent = `${connectHost}${aliasHint} (ip for ${assoc} | ${quality}${profile})`;
    } else {
      opt.textContent = `${connectHost}${aliasHint} (${target.source}${profile})`;
    }
    discoveredGroups.get(groupLabel).set(`${connectHost}\u0000${opt.textContent}`, opt);
  }

  const discovered = new Set(
    callbackTargets
      .flatMap((t) => [String(t.host || "").toLowerCase(), String(t.connect_host || "").toLowerCase()])
      .filter((v) => Boolean(v))
  );
  for (const [groupLabel, options] of discoveredGroups) {
    const group = document.createElement("optgroup");
    group.label = groupLabel;
    for (const opt of options.values()) {
      group.appendChild(opt);
    }
    hostSelectEl.appendChild(group);
  }
  const savedHosts = [];
  for (const host of manual) {
    if (discovered.has(host.toLowerCase())) {
      continue;
    }
    savedHosts.push(host);
  }
  if (savedHosts.length) {
    const savedGroup = document.createElement("optgroup");
    savedGroup.label = "Saved hosts";
    for (const host of savedHosts) {
      const opt = document.createElement("option");
      opt.value = host;
      opt.textContent = host;
      savedGroup.appendChild(opt);
    }
    hostSelectEl.appendChild(savedGroup);
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
    refreshRoute();
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
  refreshRoute();
}

function useSelectedHost() {
  const selected = hostSelectEl.value.trim();
  if (!selected) {
    jobStatusEl.textContent = "Select a discovered host first.";
    return;
  }
  hostEl.value = selected;
  syncHostIdentity();
  jobStatusEl.textContent = `Using host '${selected}'.`;
  refreshRoute();
}

function renderChecks() {
  const current = checkIdEl.value;
  checkIdEl.innerHTML = "";
  const categories = [...new Set(checksCatalog.map((item) => item.category))];
  for (const category of categories) {
    const group = document.createElement("optgroup");
    group.label = category;
    for (const item of checksCatalog.filter((check) => check.category === category)) {
      group.append(new Option(item.label, item.id));
    }
    checkIdEl.append(group);
  }
  if (checksCatalog.some((item) => item.id === current)) checkIdEl.value = current;
  taskCategoriesEl.replaceChildren();
  for (const category of ["All tasks", ...categories]) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = category;
    button.setAttribute("aria-pressed", String(category === activeCategory));
    button.addEventListener("click", () => {
      activeCategory = category;
      for (const item of taskCategoriesEl.children) item.setAttribute("aria-pressed", String(item === button));
      renderTaskCards();
    });
    taskCategoriesEl.append(button);
  }
  updateCheckDescription();
  renderTaskCards();
}

function renderTaskCards() {
  const query = taskSearchEl.value.trim().toLowerCase();
  const visible = checksCatalog.filter((item) =>
    (activeCategory === "All tasks" || item.category === activeCategory)
    && `${item.label} ${item.description} ${item.category}`.toLowerCase().includes(query));
  taskCardsEl.replaceChildren();
  for (const item of visible) {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "task-card";
    card.dataset.taskId = item.id;
    card.setAttribute("aria-pressed", String(item.id === checkIdEl.value));
    const category = document.createElement("span");
    category.className = "eyebrow";
    category.textContent = item.category;
    const title = document.createElement("strong");
    title.textContent = item.label;
    const description = document.createElement("span");
    description.className = "muted";
    description.textContent = item.description;
    card.append(category, title, description);
    card.addEventListener("click", () => {
      checkIdEl.value = item.id;
      updateCheckDescription();
    });
    taskCardsEl.append(card);
  }
  document.getElementById("task-count").textContent = visible.length
    ? `${visible.length} of ${checksCatalog.length} tasks shown.`
    : "No matching tasks. Try another category or search term.";
}

function updateCheckDescription() {
  const selected = checksCatalog.find((c) => c.id === checkIdEl.value);
  checkDescriptionEl.textContent = selected?.description || "Choose a predefined task.";
  document.getElementById("check-command").textContent = selected?.command || "Select a task to preview its command.";
  renderTaskParameters(selected);
  for (const card of taskCardsEl.children) {
    card.setAttribute("aria-pressed", String(card.dataset.taskId === checkIdEl.value));
  }
  updateRunButton();
}

function readTaskParameters() {
  return Object.fromEntries(Array.from(parametersEl.querySelectorAll("[data-parameter]"), (input) =>
    [input.dataset.parameter, input.type === "checkbox" ? input.checked : input.value]));
}

async function previewTaskCommand() {
  const requestId = ++previewRequestId;
  previewValid = false;
  updateRunButton();
  try {
    const response = await fetch("/api/test-terminal/preview", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({check_id: checkIdEl.value, parameters: readTaskParameters()}),
    });
    const data = await response.json();
    if (requestId !== previewRequestId) return;
    if (!response.ok || !data.ok) throw new Error(data.error || "Could not preview command.");
    document.getElementById("check-command").textContent = data.command;
    previewValid = true;
  } catch (error) {
    if (requestId !== previewRequestId) return;
    document.getElementById("check-command").textContent = `Cannot run: ${error.message}`;
  }
  updateRunButton();
}

function renderTaskParameters(selected) {
  previewRequestId += 1;
  clearTimeout(previewTimer);
  parametersEl.replaceChildren();
  const specs = selected?.parameters || [];
  parametersEl.hidden = !specs.length;
  kubernetesHintEl.hidden = !specs.length;
  previewValid = !specs.length;
  for (const spec of specs) {
    const field = document.createElement("div");
    field.className = "field";
    const label = document.createElement("label");
    label.htmlFor = `task-param-${spec.id}`;
    label.textContent = spec.label;
    const input = document.createElement(spec.choices ? "select" : "input");
    input.id = label.htmlFor;
    input.dataset.parameter = spec.id;
    if (spec.choices) {
      for (const choice of spec.choices) input.append(new Option(choice || "Default", choice));
    } else {
      input.type = spec.type || "text";
      input.placeholder = spec.placeholder || "";
      input.required = Boolean(spec.required);
      if (spec.min !== undefined) input.min = String(spec.min);
      if (spec.max !== undefined) input.max = String(spec.max);
      if (input.type === "number") input.step = "1";
    }
    const value = parameterValues[spec.id] ?? spec.default ?? "";
    if (input.type === "checkbox") input.checked = value === true;
    else input.value = String(value);
    input.addEventListener("input", () => {
      parameterValues[spec.id] = input.type === "checkbox" ? input.checked : input.value;
      previewRequestId += 1;
      previewValid = false;
      document.getElementById("check-command").textContent = "Updating command preview...";
      updateRunButton();
      clearTimeout(previewTimer);
      previewTimer = setTimeout(previewTaskCommand, 180);
    });
    field.append(label, input);
    parametersEl.append(field);
  }
  if (specs.length) {
    document.getElementById("check-command").textContent = "Loading command preview...";
    previewTaskCommand();
  }
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

// The dashboard links here with ?host=<node> so the form opens already pointed at that node.
function applyHostFromQuery() {
  const requested = (new URLSearchParams(window.location.search).get("host") || "").trim();
  if (!requested) return;
  const wanted = requested.toLowerCase();
  const match = callbackTargets.find((t) => [t.hostname, t.fqdn, t.host, t.connect_host].some((v) => String(v || "").trim().toLowerCase() === wanted));
  const connectHost = match ? String(match.connect_host || match.host || "").trim() : requested;
  hostEl.value = connectHost;
  if (Array.from(hostSelectEl.options).some((o) => o.value === connectHost)) {
    hostSelectEl.value = connectHost;
  }
  syncHostIdentity();
  jobStatusEl.textContent = match ? `Pre-selected ${requested} (${connectHost}).` : `'${requested}' is not a discovered target; using it as typed.`;
  refreshRoute();
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
hostEl.addEventListener("input", () => {
  hostSelectEl.value = "";
  routeRequestId += 1;
  routesBtn.disabled = true;
  routeEl.textContent = "Updating route...";
  updateRunButton();
  clearTimeout(routeTimer);
  routeTimer = setTimeout(refreshRoute, 300);
});
usernameEl.addEventListener("change", refreshRoute);
portEl.addEventListener("change", refreshRoute);
keyNameEl.addEventListener("change", updateRunButton);
taskSearchEl.addEventListener("input", renderTaskCards);
routesBtn.addEventListener("click", () => {
  const target = currentTarget();
  if (target?.hostname) NodeRoutes.open(target.hostname, resolveVerificationHost());
});
initSidebar();
refreshKeys();
refreshChecks();
renderTargets();
refreshTargets().then(applyHostFromQuery);
