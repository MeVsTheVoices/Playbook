function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/\"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function profileById(id) {
  return profiles.find((p) => p.id === id);
}

function selectedProfileIdsForNode(nodeName) {
  const node = nodes.find((n) => n.name === nodeName);
  return node && Array.isArray(node.selected_profiles) ? node.selected_profiles : [];
}

function renderNodeSelector() {
  provisionNode.innerHTML = "";
  if (!nodes.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "No active nodes";
    provisionNode.appendChild(opt);
    return;
  }

  for (const node of nodes) {
    const opt = document.createElement("option");
    opt.value = node.name;
    opt.textContent = `${node.name} (${node.group})`;
    provisionNode.appendChild(opt);
  }
}

function browserTimezone() {
  try {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    return String(tz || "").trim() || "UTC";
  } catch (_) {
    return "UTC";
  }
}

function appendModuleFieldWarning(spec, parent = moduleDetailEl) {
  if (!spec.warning) return;
  const warning = document.createElement("p");
  warning.className = "module-warning";
  warning.textContent = String(spec.warning);
  parent.appendChild(warning);
}

function appendModuleFieldHint(spec, parent = moduleDetailEl) {
  if (!spec || !spec.hint) return;
  const hint = document.createElement("p");
  hint.className = "module-hint";
  hint.textContent = String(spec.hint);
  parent.appendChild(hint);
}

// Live reads: what is actually set on the node, shown beside each saved value.
const liveConfigCache = {};
let liveConfigInFlight = "";
let liveConfigRequestId = 0;

function liveKey(nodeName, moduleId) {
  return `${nodeName}|${moduleId}`;
}

// A field the user has edited is never overwritten by a late live read.
function trackTouched(input) {
  const mark = () => { input.dataset.touched = "1"; };
  input.addEventListener("input", mark);
  input.addEventListener("change", mark);
}

function appendLiveSlot(parent, fieldId) {
  const slot = document.createElement("div");
  slot.className = "module-live";
  slot.setAttribute("data-live-for", fieldId);
  parent.appendChild(slot);
}

function buildLiveBar() {
  const bar = document.createElement("div");
  bar.className = "module-live-bar";
  const status = document.createElement("span");
  status.id = "module-live-status";
  status.className = "module-live-status idle";
  const refresh = document.createElement("button");
  refresh.type = "button";
  refresh.id = "module-live-refresh";
  refresh.className = "secondary module-live-refresh";
  refresh.textContent = "Read from node";
  refresh.addEventListener("click", () => loadLiveConfig());
  bar.append(status, refresh);
  return bar;
}

function liveAgo(iso) {
  const seconds = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (!Number.isFinite(seconds) || seconds < 10) return "just now";
  if (seconds < 90) return `${seconds}s ago`;
  return `${Math.round(seconds / 60)} min ago`;
}

function setLiveStatus(text, state, title = "") {
  const el = document.getElementById("module-live-status");
  if (!el) return;
  el.textContent = text;
  el.className = `module-live-status ${state}`;
  el.title = title;
}

function fieldValueOf(input) {
  return input.getAttribute("data-module-type") === "checkbox" ? (input.checked ? "true" : "false") : input.value.trim();
}

function setFieldValue(input, value) {
  if (input.getAttribute("data-module-type") === "checkbox") input.checked = value === "true";
  else input.value = value;
}

function applyLiveConfig(result) {
  const fields = result.fields || {};
  const readable = new Set(result.readable || []);
  moduleDetailEl.querySelectorAll("[data-module-field]").forEach((input) => {
    const id = input.getAttribute("data-module-field");
    const slot = moduleDetailEl.querySelector(`[data-live-for="${id}"]`);
    const host = input.closest(".module-field");
    if (!slot || !host) return;
    slot.className = "module-live";
    slot.replaceChildren();
    host.classList.remove("from-live");
    if (!(id in fields)) {
      // A missing checkbox only means that option does not apply here, so it gets no note.
      if (readable.has(id) && input.getAttribute("data-module-type") !== "checkbox") {
        slot.textContent = "Not found on the node";
        slot.classList.add("muted");
      }
      return;
    }
    const live = fields[id];
    if (!input.dataset.saved && !input.dataset.touched) {
      setFieldValue(input, live);
      host.classList.add("from-live");
      slot.textContent = "Filled in from the node";
      return;
    }
    if (fieldValueOf(input) === live) {
      slot.textContent = "Matches the node";
      slot.classList.add("match");
      return;
    }
    slot.classList.add("differs");
    const text = document.createElement("span");
    text.textContent = `On the node now: ${live === "" ? "(blank)" : live}`;
    const use = document.createElement("button");
    use.type = "button";
    use.className = "secondary module-live-use";
    use.textContent = "Use this";
    use.addEventListener("click", () => {
      setFieldValue(input, live);
      input.dataset.touched = "1";
      applyLiveConfig(result);
    });
    slot.append(text, use);
  });
  applyModuleRoleVisibility();
}

// Paints whatever is known about the selected node and module: in flight, cached, or not read yet.
function restoreLiveConfig() {
  const nodeName = provisionNode.value;
  const key = liveKey(nodeName, selectedModuleId);
  const refresh = document.getElementById("module-live-refresh");
  if (liveConfigInFlight === key) {
    setLiveStatus(`Reading ${nodeName}...`, "loading");
    if (refresh) refresh.disabled = true;
    return;
  }
  if (refresh) refresh.disabled = false;
  const result = liveConfigCache[key];
  if (!result) {
    setLiveStatus("Not read from the node yet", "idle");
    return;
  }
  if (!result.ok) {
    const reason = String(result.error || "unknown error");
    setLiveStatus(`Could not read ${nodeName}: ${reason.length > 120 ? `${reason.slice(0, 120)}...` : reason}`, "error", reason);
    return;
  }
  const readable = Array.isArray(result.readable) ? result.readable : [];
  if (!readable.length) {
    setLiveStatus("Nothing in this module can be read from the node", "idle");
    return;
  }
  const found = Object.keys(result.fields || {}).length;
  setLiveStatus(`Read from the node ${liveAgo(result.read_at)} \u00b7 ${found} field${found === 1 ? "" : "s"} found`, "ok", result.target || "");
  applyLiveConfig(result);
}

async function loadLiveConfig() {
  const nodeName = provisionNode.value;
  const moduleId = selectedModuleId;
  if (!nodeName || !moduleId) return;
  const key = liveKey(nodeName, moduleId);
  const requestId = ++liveConfigRequestId;
  liveConfigInFlight = key;
  restoreLiveConfig();
  let result;
  try {
    const res = await fetchJsonWithTimeout(`/api/nodes/live-config?node=${encodeURIComponent(nodeName)}&module=${encodeURIComponent(moduleId)}`, {}, 35000);
    result = await res.json();
    if (!res.ok && !result.error) result = { ok: false, error: `HTTP ${res.status}` };
  } catch (err) {
    result = { ok: false, error: err && err.name === "AbortError" ? "The read took too long." : String(err && err.message ? err.message : err) };
  }
  // A newer selection owns the screen now.
  if (requestId !== liveConfigRequestId) return;
  liveConfigInFlight = "";
  liveConfigCache[key] = result;
  if (liveKey(provisionNode.value, selectedModuleId) === key) restoreLiveConfig();
}

// Scroll the module list itself (not the page) so the module being edited stays in view.
function keepRowVisible(row) {
  const box = document.getElementById("module-list-scroll");
  if (!box || !row) return;
  const top = row.offsetTop;
  const bottom = top + row.offsetHeight;
  if (top < box.scrollTop) box.scrollTop = top;
  else if (bottom > box.scrollTop + box.clientHeight) box.scrollTop = bottom - box.clientHeight;
}

function groupDefault(spec, group) {
  const byGroup = spec.default_by_group;
  return byGroup && group in byGroup ? byGroup[group] : spec.default;
}

function applyModuleRoleVisibility() {
  const roleInput = moduleDetailEl.querySelector("[data-module-field='node_role']");
  const role = roleInput ? String(roleInput.value || "").trim().toLowerCase() : "";
  moduleDetailEl.querySelectorAll("[data-only-roles]").forEach((host) => {
    const roles = String(host.getAttribute("data-only-roles") || "").split(",").filter(Boolean);
    host.style.display = !role || roles.includes(role) ? "" : "none";
  });
  // An options block with every option hidden by the role would be a bare heading.
  moduleDetailEl.querySelectorAll(".module-options-section").forEach((section) => {
    const anyShown = Array.from(section.querySelectorAll(".module-field")).some((host) => host.style.display !== "none");
    section.style.display = anyShown ? "" : "none";
  });
}

function renderModuleSection() {
  if (!moduleStatusBodyEl || !moduleDetailEl) {
    return;
  }
  const nodeName = provisionNode.value;
  const selected = new Set(selectedProfileIdsForNode(nodeName));
  const selectedNode = nodes.find((n) => n.name === nodeName) || {};
  const savedTailscale = nodeInputs[nodeName]?.tailscale || {};
  const familyRole = selectedNode.group === "rpi_3bplus_active" ? "light-worker" : "worker";
  const moduleConnectionContext = document.getElementById("module-connection-context");
  if (moduleConnectionContext) {
    const adminPath = String(selectedNode.address_source || "inventory");
    const adminAddress = String(selectedNode.address || "unknown");
    const clusterIp = String(savedTailscale.node_ip || "not configured");
    moduleConnectionContext.textContent = `Admin SSH target: ${adminAddress} (${adminPath}). K3s peer/API path: Tailscale (${clusterIp}); node role is derived from Pi family.`;
  }
  moduleStatusBodyEl.innerHTML = "";
  let selectedRow = null;
  if (!profiles.length) {
    moduleStatusBodyEl.innerHTML = "<tr><td colspan='4' style='color:var(--muted)'>No modules available.</td></tr>";
  }
  for (const profile of profiles) {
    const isActive = selected.has(profile.id);
    const tags = Array.isArray(profile.tags) ? profile.tags.join(", ") : "-";
    const tr = document.createElement("tr");
    const activePill = isActive
      ? "<span class='status-chip ok'>active</span>"
      : "<span class='status-chip'>inactive</span>";
    tr.innerHTML = `
      <td><strong>${escapeHtml(profile.label || profile.id)}</strong></td>
      <td class='mono'>${escapeHtml(tags)}</td>
      <td>${activePill}</td>
      <td><button type='button' class='node-action-btn secondary module-configure-btn'>${profile.id === selectedModuleId ? "Editing" : "Configure"}</button></td>
    `;
    tr.className = profile.id === selectedModuleId ? "module-row module-row-selected" : "module-row";
    const selectModule = () => {
      selectedModuleId = profile.id;
      moduleStatusEl.textContent = "";
      renderModuleSection();
      loadLiveConfig();
    };
    tr.querySelector(".module-configure-btn").addEventListener("click", (event) => {
      event.stopPropagation();
      selectModule();
    });
    tr.addEventListener("click", () => {
      if (profile.id !== selectedModuleId) selectModule();
    });
    moduleStatusBodyEl.appendChild(tr);
    if (profile.id === selectedModuleId) selectedRow = tr;
  }
  keepRowVisible(selectedRow);

  const profile = profiles.find((p) => p.id === selectedModuleId);
  if (!profile) {
    moduleDetailEl.classList.add("collapsed");
    moduleDetailEl.innerHTML = "<p class='note'>Select a module from the table to view its parameter template.</p>";
    renderModulePresets();
    return;
  }
  const saved = (nodeInputs[nodeName] && typeof nodeInputs[nodeName] === "object" && typeof nodeInputs[nodeName][profile.id] === "object")
    ? nodeInputs[nodeName][profile.id]
    : {};
  const specs = (Array.isArray(profile.input_specs) ? profile.input_specs : []).filter((spec) => (
    !Array.isArray(spec.only_groups) || spec.only_groups.includes(selectedNode.group)
  ));
  const groups = Array.isArray(profile.groups) ? profile.groups : [];
  const tagsHtml = (Array.isArray(profile.tags) ? profile.tags : []).map((t) => `<span class="module-tag">${escapeHtml(t)}</span>`).join("");
  const groupsHtml = groups.map((g) => `<span class="module-tag">${escapeHtml(g)}</span>`).join("");

  moduleDetailEl.classList.remove("collapsed");
  moduleDetailEl.innerHTML = "";
  const head = document.createElement("div");
  head.className = "module-head";
  const headMain = document.createElement("div");
  const heading = document.createElement("h4");
  heading.textContent = profile.label || profile.id;
  headMain.appendChild(heading);
  const tagRow = document.createElement("div");
  tagRow.className = "module-tags";
  tagRow.innerHTML = tagsHtml + groupsHtml;
  headMain.appendChild(tagRow);
  head.append(headMain, buildLiveBar());
  moduleDetailEl.appendChild(head);
  const desc = document.createElement("p");
  desc.className = "module-desc";
  desc.textContent = profile.description || "";
  moduleDetailEl.appendChild(desc);
  if (profile.id === "kubernetes") {
    const roleHint = document.createElement("p");
    roleHint.className = "module-callout";
    const boardName = selectedNode.group === "rpi_3bplus_active" ? "Pi 3B+" : (selectedNode.group === "rpi4_active" ? "Pi 4" : "Pi 5");
    roleHint.textContent = `Rack layout: the NucBox is the K3s server. This ${selectedNode.group === "rpi_3bplus_active" ? "Pi 3B+ defaults to 'light-worker' (a tainted K3s agent; it is too small to carry etcd)" : boardName + " defaults to 'worker'"}. Any Pi can instead be 'utility' (no Kubernetes) or 'control-plane' (a K3s server and etcd voter; keep the number of servers odd${selectedNode.group === "rpi4_active" ? "; boot a Pi 4 control-plane from an SSD, not an SD card" : ""}).`;
    moduleDetailEl.appendChild(roleHint);
  }
  if (profile.id === "longhorn") {
    const helper = document.createElement("p");
    helper.className = "module-callout";
    helper.textContent = "Use a stable mounted path for Longhorn data now; when you move the Pi boot/storage layout to NVMe later, change only the storage path value rather than the workflow. Storage Disk 'auto' formats and mounts a single blank disk at that path for you (a disk that already holds data is never touched). Recommended first pass: keep the default replica count at 2 on a 2-node storage pair.";
    moduleDetailEl.appendChild(helper);
  }
  if (!specs.length) {
    const empty = document.createElement("p");
    empty.className = "note";
    empty.textContent = "This module has no parameters to edit.";
    moduleDetailEl.appendChild(empty);
  }
  const contextDefaults = {
    "kubernetes.node_role": familyRole,
    "kubernetes.node_ip": String(savedTailscale.node_ip || ""),
    "baseline.timezone": browserTimezone(),
  };
  const fieldsGrid = document.createElement("div");
  fieldsGrid.className = "module-fields";
  moduleDetailEl.appendChild(fieldsGrid);
  const optionsGrid = document.createElement("div");
  optionsGrid.className = "module-options";
  const optionsSection = document.createElement("section");
  optionsSection.className = "module-options-section";
  const optionsTitle = document.createElement("h5");
  optionsTitle.textContent = "Options";
  optionsSection.append(optionsTitle, optionsGrid);
  moduleDetailEl.appendChild(optionsSection);
  for (const spec of specs) {
    const fieldId = String(spec.id || "");
    if (!fieldId) continue;
    const inputType = String(spec.input_type || "text").toLowerCase();
    const savedValue = String(saved[fieldId] || "");
    const fieldHost = document.createElement("div");
    fieldHost.className = "module-field";
    if (Array.isArray(spec.only_roles)) fieldHost.setAttribute("data-only-roles", spec.only_roles.join(","));
    const requiredMark = spec.required ? " <span class='required'>*</span>" : "";

    if (inputType === "checkbox") {
      fieldHost.classList.add("module-toggle");
      optionsGrid.appendChild(fieldHost);
      const row = document.createElement("label");
      row.className = "module-toggle-row";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.setAttribute("data-module-field", fieldId);
      input.setAttribute("data-module-type", "checkbox");
      input.dataset.saved = savedValue ? "1" : "";
      input.checked = savedValue
        ? ["1", "true", "yes", "on"].includes(savedValue.toLowerCase())
        : ["1", "true", "yes", "on"].includes(String(groupDefault(spec, selectedNode.group)).toLowerCase());
      trackTouched(input);
      const text = document.createElement("span");
      text.innerHTML = `${escapeHtml(spec.label || fieldId)}${requiredMark}`;
      row.append(input, text);
      fieldHost.appendChild(row);
      appendModuleFieldHint(spec, fieldHost);
      appendModuleFieldWarning(spec, fieldHost);
      appendLiveSlot(fieldHost, fieldId);
      continue;
    }

    fieldsGrid.appendChild(fieldHost);
    if (inputType === "textarea" || String(spec.hint || "").length > 140) fieldHost.classList.add("wide");
    const label = document.createElement("label");
    label.htmlFor = `module-field-${fieldId}`;
    label.innerHTML = `${escapeHtml(spec.label || fieldId)}${requiredMark}`;
    fieldHost.appendChild(label);

    const configuredSecret = Boolean(spec.sensitive) && savedValue === CONFIGURED_SECRET_MARKER;
    let input;
    if (inputType === "select") {
      input = document.createElement("select");
      const byGroup = spec.options_by_group && spec.options_by_group[selectedNode.group];
      const options = Array.isArray(byGroup) ? byGroup : (Array.isArray(spec.options) ? spec.options : []);
      for (const optionValue of options) {
        const option = document.createElement("option");
        option.value = String(optionValue);
        option.textContent = String(optionValue);
        input.appendChild(option);
      }
    } else if (inputType === "textarea") {
      input = document.createElement("textarea");
      input.rows = 5;
    } else {
      input = document.createElement("input");
      input.type = spec.sensitive ? "password" : "text";
    }
    input.placeholder = configuredSecret ? "Configured — leave blank to keep saved secret" : (spec.placeholder || "");
    input.value = configuredSecret ? "" : savedValue;
    if (!savedValue) {
      const contextKey = `${profile.id}.${fieldId}`;
      if (contextKey in contextDefaults) {
        input.value = contextDefaults[contextKey];
      } else if (spec.default != null) {
        input.value = String(groupDefault(spec, selectedNode.group));
      }
    }
    input.setAttribute("data-module-field", fieldId);
    input.setAttribute("data-module-type", inputType);
    input.id = `module-field-${fieldId}`;
    input.dataset.saved = savedValue ? "1" : "";
    trackTouched(input);
    fieldHost.appendChild(input);
    if (inputType === "select") {
      const groupDefault = spec.default_by_group && spec.default_by_group[selectedNode.group];
      const allowed = Array.from(input.options).map((o) => o.value);
      // A value saved under an older layout (e.g. 'worker' on a Pi 3B+) is no longer offered.
      if (!allowed.includes(input.value)) input.value = allowed.includes(groupDefault) ? groupDefault : (allowed[0] || "");
      input.addEventListener("change", applyModuleRoleVisibility);
    }
    if (spec.check && window.attachLongformCheck) window.attachLongformCheck(input, spec.check);
    appendModuleFieldHint(spec, fieldHost);
    appendModuleFieldWarning(spec, fieldHost);
    appendLiveSlot(fieldHost, fieldId);
  }
  if (!optionsGrid.children.length) optionsSection.remove();
  if (profile.id === "telegraf") {
    const testRow = document.createElement("div");
    testRow.className = "inline-actions module-test-row";
    const testButton = document.createElement("button");
    testButton.type = "button";
    testButton.className = "secondary";
    testButton.id = "telegraf-test-influx";
    testButton.textContent = "Test Influx connection";
    const testResult = document.createElement("p");
    testResult.className = "note";
    testResult.id = "telegraf-test-result";
    testButton.addEventListener("click", () => testInfluxConnection(testButton, testResult));
    testRow.appendChild(testButton);
    moduleDetailEl.appendChild(testRow);
    moduleDetailEl.appendChild(testResult);
  }
  applyModuleRoleVisibility();
  renderModulePresets();
  restoreLiveConfig();
}

function collectModuleInputs() {
  const out = {};
  moduleDetailEl.querySelectorAll("[data-module-field]").forEach((input) => {
    if (input.getAttribute("data-module-type") === "checkbox") {
      // Explicit "false" (not blank) so a default-on option stays off after saving.
      out[input.getAttribute("data-module-field")] = input.checked ? "true" : "false";
      return;
    }
    out[input.getAttribute("data-module-field")] = input.value.trim();
  });
  return out;
}

// Writes one dummy point to Influx with the entered values (saved secrets fill blank secret fields) and keeps the token form that worked.
async function testInfluxConnection(button, resultEl) {
  const nodeName = provisionNode.value;
  if (!nodeName) {
    resultEl.textContent = "Select a node first.";
    return;
  }
  button.disabled = true;
  resultEl.textContent = "Testing the Influx connection...";
  try {
    const res = await fetchJsonWithTimeout("/api/nodes/telegraf/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node: nodeName, profile_inputs: { telegraf: collectModuleInputs() } }),
    }, 30000);
    const data = await res.json();
    const result = data.result || {};
    const checks = (result.checks || []).map((c) => `${c.name}: ${c.ok ? "ok" : `failed${c.error ? ` (${c.error})` : ""}`}`);
    if (!data.ok) {
      const missing = (result.missing || []).length ? ` Missing: ${result.missing.join(", ")}.` : "";
      resultEl.textContent = `${result.error || data.error || "Influx test failed."}${missing} ${checks.join(" · ")}`.trim();
      return;
    }
    const modeInput = moduleDetailEl.querySelector("[data-module-field='influx_token_mode']");
    if (modeInput && result.auth_mode) modeInput.value = result.auth_mode;
    resultEl.textContent = `${checks.join(" · ")}. Token form set to ${result.auth_mode || "access_key"}; save the module inputs to keep it.`;
  } catch (err) {
    resultEl.textContent = `Influx test failed: ${String(err && err.message ? err.message : err)}`;
  } finally {
    button.disabled = false;
  }
}

async function saveModuleInputs() {
  const nodeName = provisionNode.value;
  if (!nodeName) {
    moduleStatusEl.textContent = "Select a node first.";
    return false;
  }
  if (!selectedModuleId) {
    moduleStatusEl.textContent = "Select a module first.";
    return false;
  }

  const selected = new Set(selectedProfileIdsForNode(nodeName));
  if (!selected.has(selectedModuleId)) {
    selected.add(selectedModuleId);
    const res = await fetch("/api/nodes/node-configs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node: nodeName, profile_ids: Array.from(selected) }),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      moduleStatusEl.textContent = data.error || "Could not add the module to the node selection.";
      return false;
    }
  }

  const res = await fetch("/api/nodes/node-inputs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ node: nodeName, profile_inputs: { [selectedModuleId]: collectModuleInputs() } }),
  });
  const data = await res.json();
  if (!res.ok || !data.ok) {
    moduleStatusEl.textContent = data.error || "Could not save module inputs.";
    return false;
  }
  moduleStatusEl.textContent = `Saved inputs for module '${selectedModuleId}'.`;
  await loadSummary();
  return true;
}

function checkedModulePlaceholder() {
  return "";
}
