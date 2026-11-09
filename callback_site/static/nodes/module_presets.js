// Saved module configurations: remembered saves, named presets, and values drawn from Node Ops or the provisioning dashboard.

const modulePresetsEl = document.getElementById("module-presets");
let modulePresetRenderToken = 0;
const moduleNameToggle = document.getElementById("module-name-toggle");
const moduleNameForm = document.getElementById("module-name-form");
const moduleNameInput = document.getElementById("module-preset-name");
const moduleNameCancel = document.getElementById("module-name-cancel");
let moduleNameContext = "";
let moduleNameSaving = false;

function closeModuleNameForm(restoreFocus = false) {
  moduleNameForm.hidden = true;
  moduleNameToggle.hidden = false;
  moduleNameToggle.setAttribute("aria-expanded", "false");
  moduleNameInput.value = "";
  if (restoreFocus) moduleNameToggle.focus();
}

moduleNameToggle.addEventListener("click", () => {
  if (moduleNameSaving) return;
  moduleNameToggle.hidden = true;
  moduleNameForm.hidden = false;
  moduleNameToggle.setAttribute("aria-expanded", "true");
  moduleNameInput.focus();
});
moduleNameCancel.addEventListener("click", () => closeModuleNameForm(true));
moduleNameForm.addEventListener("keydown", event => {
  if (event.key === "Escape" && !moduleNameSaving) {
    event.preventDefault();
    closeModuleNameForm(true);
  }
});
moduleNameForm.addEventListener("submit", async event => {
  event.preventDefault();
  if (moduleNameSaving) return;
  moduleNameSaving = true;
  moduleNameToggle.disabled = true;
  const context = moduleNameContext;
  let saved = false;
  for (const control of moduleNameForm.elements) control.disabled = true;
  moduleNameForm.setAttribute("aria-busy", "true");
  try {
    saved = await saveNamedConfiguration(moduleNameInput.value.trim());
  } catch (error) {
    moduleStatusEl.textContent = `Could not save named configuration: ${error.message}`;
  } finally {
    moduleNameSaving = false;
    moduleNameToggle.disabled = !provisionNode.value || !selectedModuleId;
    for (const control of moduleNameForm.elements) control.disabled = false;
    moduleNameForm.removeAttribute("aria-busy");
  }
  if (saved && context === moduleNameContext) closeModuleNameForm(true);
});

const PRESET_ORIGIN_LABELS = {
  "node-ops": "Node Ops",
  "provisioning-dashboard": "Provisioning dashboard",
};

function presetWhen(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString();
}

function presetFieldsText(fields) {
  if (!fields || !fields.length) return "no values";
  return fields.map((f) => `${f.label}: ${f.sensitive ? "(secret kept)" : f.value}`).join(" · ");
}

async function postJson(url, body, method = "POST") {
  const res = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  const data = await res.json();
  return { ok: res.ok && data.ok, data };
}

async function loadModuleSource(entry, thenApply) {
  const nodeName = provisionNode.value;
  const moduleId = selectedModuleId;
  moduleStatusEl.textContent = "Loading configuration...";
  const { ok, data } = await postJson("/api/nodes/module-presets/load", { node: nodeName, module: moduleId, source: entry.source });
  if (!ok) {
    moduleStatusEl.textContent = data.error || "Could not load that configuration.";
    return;
  }
  await loadSummary();
  moduleStatusEl.textContent = `Loaded ${data.label} into ${nodeName} (${data.filled.length} field(s): ${data.filled.join(", ")}). Not applied yet.`;
  if (thenApply) await runProvision(nodeName, "apply", [moduleId]);
}

async function renamePreset(entry) {
  const name = window.prompt("Name for this configuration (leave empty to go back to the suggested name):", entry.name || entry.auto_name || "");
  if (name === null) return;
  const { ok, data } = await postJson("/api/nodes/module-presets", { id: entry.id, name });
  moduleStatusEl.textContent = ok ? (data.name ? `Named configuration '${data.name}'.` : "Configuration unnamed.") : (data.error || "Could not rename.");
  await renderModulePresets();
}

async function deletePreset(entry) {
  if (!window.confirm(`Delete ${entry.name ? `'${entry.name}'` : "this saved configuration"}?`)) return;
  const { ok, data } = await postJson(`/api/nodes/module-presets/${encodeURIComponent(entry.id)}`, null, "DELETE");
  moduleStatusEl.textContent = ok ? "Saved configuration deleted." : (data.error || "Could not delete.");
  await renderModulePresets();
}

async function saveNamedConfiguration(name) {
  const node = provisionNode.value;
  const module = selectedModuleId;
  const saved = await saveModuleInputs();
  if (!saved) return false;
  const { ok, data } = await postJson("/api/nodes/module-presets", { node, module, name });
  moduleStatusEl.textContent = ok ? `Saved the current inputs as '${data.name}'.` : (data.error || "Could not name the configuration.");
  await renderModulePresets();
  return ok;
}

function presetButton(label, handler, secondary = true) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = secondary ? "node-action-btn secondary" : "node-action-btn";
  button.textContent = label;
  button.addEventListener("click", handler);
  return button;
}

function presetTable(entries, { editable }) {
  const table = document.createElement("table");
  table.innerHTML = "<thead><tr><th>Configuration</th><th>Values</th><th>Action</th></tr></thead>";
  const body = document.createElement("tbody");
  for (const entry of entries) {
    const tr = document.createElement("tr");
    const title = document.createElement("td");
    const strong = document.createElement("strong");
    strong.textContent = editable ? (entry.name || entry.auto_name || "Unnamed save") : entry.label;
    title.appendChild(strong);
    const meta = document.createElement("div");
    meta.className = "note";
    const origin = PRESET_ORIGIN_LABELS[entry.origin] || entry.origin;
    const where = editable && entry.node ? ` · saved on ${entry.node}` : "";
    const suggested = editable && !entry.name ? " · suggested name" : "";
    meta.textContent = [origin + where + suggested, presetWhen(entry.saved_at)].filter(Boolean).join(" · ");
    title.appendChild(meta);

    const values = document.createElement("td");
    values.className = "mono";
    values.textContent = presetFieldsText(entry.fields);

    const actions = document.createElement("td");
    actions.className = "inline-actions";
    actions.appendChild(presetButton("Load", () => loadModuleSource(entry, false)));
    actions.appendChild(presetButton("Apply", () => loadModuleSource(entry, true), false));
    if (editable) {
      actions.appendChild(presetButton(entry.name ? "Rename" : "Name…", () => renamePreset(entry)));
      actions.appendChild(presetButton("Delete", () => deletePreset(entry)));
    }
    tr.append(title, values, actions);
    body.appendChild(tr);
  }
  table.appendChild(body);
  return table;
}

function presetHeading(text, note) {
  const wrap = document.createElement("div");
  const h = document.createElement("h4");
  h.textContent = text;
  h.style.marginBottom = "0.2rem";
  const p = document.createElement("p");
  p.className = "note";
  p.textContent = note;
  wrap.append(h, p);
  return wrap;
}

async function renderModulePresets() {
  if (!modulePresetsEl) return;
  const nodeName = provisionNode.value;
  const moduleId = selectedModuleId;
  const context = `${nodeName}:${moduleId}`;
  if (context !== moduleNameContext) {
    closeModuleNameForm();
    moduleNameContext = context;
  }
  moduleNameToggle.disabled = moduleNameSaving || !nodeName || !moduleId;
  const token = ++modulePresetRenderToken;
  if (!nodeName || !moduleId) {
    modulePresetsEl.innerHTML = "";
    return;
  }
  let data;
  try {
    const res = await fetch(`/api/nodes/module-presets?module=${encodeURIComponent(moduleId)}&node=${encodeURIComponent(nodeName)}`);
    data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || "request failed");
  } catch (err) {
    if (token === modulePresetRenderToken) modulePresetsEl.textContent = `Could not load saved configurations: ${err.message}`;
    return;
  }
  if (token !== modulePresetRenderToken) return;

  modulePresetsEl.innerHTML = "";
  modulePresetsEl.style.marginTop = "1rem";
  if (data.presets.length) {
    modulePresetsEl.appendChild(presetHeading("Saved configurations", "Load fills this node's form; Apply also pushes it to the node."));
    modulePresetsEl.appendChild(presetTable(data.presets, { editable: true }));
  }

  modulePresetsEl.appendChild(presetHeading("Draw from existing settings", "Values already enabled elsewhere: other nodes in Node Ops, and SD-card profiles from the provisioning dashboard. Only fields this module uses are listed; secrets are copied but never shown."));
  if (data.sources.length) {
    modulePresetsEl.appendChild(presetTable(data.sources, { editable: false }));
  } else {
    const none = document.createElement("p");
    none.className = "note";
    none.textContent = "No other node or SD profile has settings for this module.";
    modulePresetsEl.appendChild(none);
  }
}
