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
  warning.className = "note";
  warning.style.color = "#b45309";
  warning.textContent = `Warning: ${spec.warning}`;
  parent.appendChild(warning);
}

function appendModuleFieldHint(spec, parent = moduleDetailEl) {
  if (!spec || !spec.hint) return;
  const hint = document.createElement("p");
  hint.className = "note";
  hint.style.margin = "0 0 0.3rem 1.6rem";
  hint.textContent = String(spec.hint);
  parent.appendChild(hint);
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
    tr.querySelector(".module-configure-btn").addEventListener("click", () => {
      selectedModuleId = profile.id;
      moduleStatusEl.textContent = "";
      renderModuleSection();
    });
    moduleStatusBodyEl.appendChild(tr);
  }

  const profile = profiles.find((p) => p.id === selectedModuleId);
  if (!profile) {
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

  moduleDetailEl.innerHTML = "";
  const heading = document.createElement("h4");
  heading.textContent = profile.label || profile.id;
  moduleDetailEl.appendChild(heading);
  const desc = document.createElement("p");
  desc.className = "note";
  desc.textContent = profile.description || "";
  moduleDetailEl.appendChild(desc);
  if (profile.id === "kubernetes") {
    const roleHint = document.createElement("p");
    roleHint.className = "note";
    const boardName = selectedNode.group === "rpi_3bplus_active" ? "Pi 3B+" : (selectedNode.group === "rpi4_active" ? "Pi 4" : "Pi 5");
    roleHint.textContent = `Rack layout: the NucBox is the K3s server. This ${selectedNode.group === "rpi_3bplus_active" ? "Pi 3B+ defaults to 'light-worker' (a tainted K3s agent; it is too small to carry etcd)" : boardName + " defaults to 'worker'"}. Any Pi can instead be 'utility' (no Kubernetes) or 'control-plane' (a K3s server and etcd voter; keep the number of servers odd${selectedNode.group === "rpi4_active" ? "; boot a Pi 4 control-plane from an SSD, not an SD card" : ""}).`;
    moduleDetailEl.appendChild(roleHint);
  }
  if (profile.id === "longhorn") {
    const helper = document.createElement("p");
    helper.className = "note";
    helper.textContent = "Use a stable mounted path for Longhorn data now; when you move the Pi boot/storage layout to NVMe later, change only the storage path value rather than the workflow. Recommended first pass: keep the default replica count at 2 on a 2-node storage pair.";
    moduleDetailEl.appendChild(helper);
  }
  const tagRow = document.createElement("div");
  tagRow.className = "module-tags";
  tagRow.innerHTML = tagsHtml + groupsHtml;
  moduleDetailEl.appendChild(tagRow);

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
  for (const spec of specs) {
    const fieldId = String(spec.id || "");
    if (!fieldId) continue;
    const inputType = String(spec.input_type || "text").toLowerCase();
    const savedValue = String(saved[fieldId] || "");
    const fieldHost = document.createElement("div");
    if (Array.isArray(spec.only_roles)) fieldHost.setAttribute("data-only-roles", spec.only_roles.join(","));
    moduleDetailEl.appendChild(fieldHost);

    if (inputType === "checkbox") {
      const row = document.createElement("div");
      row.className = "checkbox-row";
      row.style.marginTop = "0.35rem";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.style.width = "auto";
      input.setAttribute("data-module-field", fieldId);
      input.setAttribute("data-module-type", "checkbox");
      input.checked = savedValue
        ? ["1", "true", "yes", "on"].includes(savedValue.toLowerCase())
        : ["1", "true", "yes", "on"].includes(String(groupDefault(spec, selectedNode.group)).toLowerCase());
      const label = document.createElement("label");
      label.style.margin = "0 0 0 0.4rem";
      label.innerHTML = `${escapeHtml(spec.label || fieldId)}${spec.required ? " *" : ""}`;
      row.appendChild(input);
      row.appendChild(label);
      if (spec.hint) row.title = String(spec.hint);
      fieldHost.appendChild(row);
      appendModuleFieldHint(spec, fieldHost);
      appendModuleFieldWarning(spec, fieldHost);
      continue;
    }

    const label = document.createElement("label");
    label.style.marginTop = "0.4rem";
    label.innerHTML = `${escapeHtml(spec.label || fieldId)}${spec.required ? " *" : ""}`;
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
  }
  applyModuleRoleVisibility();
  renderModulePresets();
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
