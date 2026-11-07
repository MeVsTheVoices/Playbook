// A node's current group is always selectable, even if the backend list does not name it.
function setManualNodeGroup(value) {
  const group = String(value || "").trim() || "manual";
  if (!Array.from(manualNodeGroupEl.options).some((option) => option.value === group)) {
    const opt = document.createElement("option");
    opt.value = group;
    opt.textContent = group;
    manualNodeGroupEl.appendChild(opt);
  }
  manualNodeGroupEl.value = group;
}

async function refreshNodeGroups() {
  const keep = manualNodeGroupEl.value || "manual";
  try {
    const res = await fetch("/api/nodes/groups");
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || "Failed to load groups");
    manualNodeGroupEl.innerHTML = "";
    for (const group of data.groups || []) {
      const opt = document.createElement("option");
      opt.value = group.name;
      opt.textContent = group.description ? `${group.name} \u2014 ${group.description}` : group.name;
      manualNodeGroupEl.appendChild(opt);
    }
  } catch (err) {
    manualNodeStatus.textContent = `Group list error: ${String(err.message || err)}`;
    return;
  }
  setManualNodeGroup(keep);
}

function resetManualNodeForm() {
  manualNodeNameEl.value = "";
  setManualNodeGroup("manual");
  manualNodeAddressEl.value = "";
  manualNodeUsernameEl.value = "";
  manualNodePasswordEl.value = "";
  manualNodeShellEl.value = "";
  manualNodeCertificateEl.value = "";
  const helper = document.getElementById("manual-node-helper");
  if (helper) {
    helper.textContent = "Choose a discovered or saved node to edit its identity, or leave this blank to add a fresh one.";
    helper.classList.remove("manual-node-helper-active");
  }
}

function renderManualNodeSelector() {
  const currentSelection = manualNodeSelectorEl.value;
  manualNodeSelectorEl.innerHTML = '<option value="">Create new node</option>';

  const seenNames = new Set();
  const candidateNames = [];
  for (const node of nodes) {
    const name = String(node?.name || "").trim();
    if (!name || seenNames.has(name)) continue;
    seenNames.add(name);
    candidateNames.push(name);
  }

  for (const name of Object.keys(manualNodes || {}).sort((a, b) => a.localeCompare(b))) {
    if (!seenNames.has(name)) {
      seenNames.add(name);
      candidateNames.push(name);
    }
  }

  for (const name of candidateNames.sort((a, b) => a.localeCompare(b))) {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    manualNodeSelectorEl.appendChild(opt);
  }

  if (currentSelection && candidateNames.includes(currentSelection)) {
    manualNodeSelectorEl.value = currentSelection;
  } else {
    manualNodeSelectorEl.value = "";
  }
}

function fillManualNodeForm(nodeName) {
  const entry = manualNodes[nodeName];
  const node = nodes.find((item) => item.name === nodeName);
  const source = entry || node || null;
  if (!source) {
    resetManualNodeForm();
    return;
  }
  manualNodeNameEl.value = source.name || nodeName || "";
  setManualNodeGroup(source.group || entry?.group);
  manualNodeAddressEl.value = source.address || "";
  manualNodeUsernameEl.value = source.username || source.node_username || "";
  manualNodePasswordEl.value = "";
  manualNodeShellEl.value = source.default_shell || "";
  manualNodeCertificateEl.value = source.certificate || "";
  const helper = document.getElementById("manual-node-helper");
  if (helper) {
    helper.textContent = `Editing ${nodeName}. Tweak the fields and save to refresh that node’s identity.`;
    helper.classList.remove("manual-node-helper-active");
    void helper.offsetWidth;
    helper.classList.add("manual-node-helper-active");
  }
}

async function refreshCertificates() {
  try {
    const res = await fetch("/api/ssh/keys");
    const data = await res.json();
    manualNodeCertificateEl.innerHTML = "";

    const noneOption = document.createElement("option");
    noneOption.value = "";
    noneOption.textContent = "None selected";
    manualNodeCertificateEl.appendChild(noneOption);

    if (!res.ok || !data.ok) {
      throw new Error(data.error || "Failed to load certificates");
    }
    const keys = Array.isArray(data.keys) ? data.keys : [];
    for (const key of keys) {
      const keyName = String(key.key_name || "").trim();
      if (!keyName) continue;
      const opt = document.createElement("option");
      opt.value = keyName;
      opt.textContent = keyName;
      manualNodeCertificateEl.appendChild(opt);
    }
  } catch (err) {
    manualNodeStatus.textContent = `Certificate refresh error: ${String(err.message || err)}`;
  }
}

async function addManualNode() {
  const selectedNode = manualNodeSelectorEl.value.trim();
  const selectedCertificate = manualNodeCertificateEl.value.trim();
  const payload = {
    name: manualNodeNameEl.value.trim() || selectedNode,
    group: manualNodeGroupEl.value.trim() || "manual",
    address: manualNodeAddressEl.value.trim(),
    username: manualNodeUsernameEl.value.trim(),
    password: manualNodePasswordEl.value,
    default_shell: manualNodeShellEl.value.trim(),
    certificate: selectedCertificate,
  };
  if (!payload.name) {
    manualNodeStatus.textContent = "Node name is required.";
    manualNodeNameEl.focus();
    return;
  }
  if (!selectedCertificate) {
    manualNodeStatus.textContent = "Select a certificate / SSH key before saving the manual node.";
    manualNodeCertificateEl.focus();
    return;
  }

  manualNodeStatus.textContent = "Saving manual node...";
  try {
    const res = await fetch("/api/nodes/manual-nodes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      throw new Error(data.error || "Failed to save manual node");
    }
    manualNodeStatus.textContent = `Saved manual node '${payload.name}'.`;
    const actionButton = document.getElementById("add-manual-node");
    actionButton.classList.remove("manual-node-burst");
    void actionButton.offsetWidth;
    actionButton.classList.add("manual-node-burst");
    manualNodePasswordEl.value = "";
    if (selectedNode) {
      manualNodeSelectorEl.value = payload.name;
      fillManualNodeForm(payload.name);
    } else {
      manualNodeSelectorEl.value = "";
      resetManualNodeForm();
    }
    await loadSummary();
  } catch (err) {
    manualNodeStatus.textContent = `Manual node save error: ${String(err.message || err)}`;
  }
}

async function deleteManualNode(nodeName) {
  if (!nodeName) return;
  if (!confirm(`Remove manual node '${nodeName}'?`)) {
    return;
  }
  manualNodeStatus.textContent = `Removing manual node '${nodeName}'...`;
  try {
    const res = await fetch(`/api/nodes/manual-nodes/${encodeURIComponent(nodeName)}`, {
      method: "DELETE",
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      throw new Error(data.error || "Failed to remove manual node");
    }
    manualNodeStatus.textContent = `Removed manual node '${nodeName}'.`;
    await loadSummary();
  } catch (err) {
    manualNodeStatus.textContent = `Manual node delete error: ${String(err.message || err)}`;
  }
}
