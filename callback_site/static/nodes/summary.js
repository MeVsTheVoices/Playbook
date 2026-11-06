async function pruneStaleNodes() {
  const hoursRaw = window.prompt("Remove nodes with callbacks older than how many hours?", "168");
  if (hoursRaw == null) {
    return;
  }
  const hours = Number.parseInt(String(hoursRaw).trim(), 10);
  if (!Number.isFinite(hours) || hours < 1 || hours > 8760) {
    monitorStatus.textContent = "Stale threshold must be an integer between 1 and 8760 hours.";
    return;
  }
  if (!window.confirm(`Prune stale nodes older than ${hours} hour(s)? This removes callback and saved node state for those nodes.`)) {
    return;
  }

  monitorStatus.textContent = "Pruning stale nodes...";
  try {
    const res = await fetch("/api/nodes/stale/prune", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ max_age_hours: hours, dry_run: false }),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      throw new Error(data.error || "Failed to prune stale nodes");
    }
    const staleNodes = Array.isArray(data.stale_nodes) ? data.stale_nodes : [];
    monitorStatus.textContent = `Pruned ${staleNodes.length} stale node${staleNodes.length === 1 ? "" : "s"}.`;
    await loadSummary();
  } catch (err) {
    monitorStatus.textContent = `Stale prune error: ${String(err.message || err)}`;
  }
}

async function fetchNodeState() {
  const sotRes = await fetch("/api/state/source-of-truth");
  const sotData = await sotRes.json();
  if (!sotRes.ok || !sotData.ok) {
    return null;
  }
  return sotData.state || {};
}

// Keeps reachability already measured for a node unless its address changed underneath it.
function applyNodeState(state) {
  nodes = Array.isArray(state.nodes) ? state.nodes : [];
  profiles = Array.isArray(state.profiles) ? state.profiles : [];
  nodeInputs = (state.provisioning && typeof state.provisioning.node_inputs === "object") ? state.provisioning.node_inputs : {};
  manualNodes = (state.provisioning && typeof state.provisioning.manual_nodes === "object") ? state.provisioning.manual_nodes : {};

  const previous = statusByNode;
  statusByNode = {};
  for (const node of nodes) {
    const prior = previous[node.name];
    const sameAddress = prior && prior.address === node.address;
    statusByNode[node.name] = {
      name: node.name,
      address: node.address,
      address_source: node.address_source || "inventory",
      reachable: sameAddress ? prior.reachable : null,
      latency_ms: sameAddress ? prior.latency_ms : null,
      last_callback: node.last_callback || "",
      error: sameAddress ? prior.error : "",
    };
  }
}

async function loadSummary() {
  const state = await fetchNodeState();
  if (!state) {
    monitorStatus.textContent = "Failed to load node summary.";
    return;
  }

  applyNodeState(state);
  renderProfileSegmentOptions();
  renderManualNodeSelector();
  renderOpenwrtHosts();

  const config = (state.provisioning && state.provisioning.config) || {};
  document.getElementById("username").value = config.username || "";
  document.getElementById("webhook_url").value = config.webhook_url || "";
  document.getElementById("verify_tls").value = config.verify_tls ? "true" : "false";
  if (config.password_set) {
    document.getElementById("password").placeholder = "already configured";
  }

  const oldNode = provisionNode.value;
  renderNodeSelector();
  if (oldNode && nodes.some((n) => n.name === oldNode)) {
    provisionNode.value = oldNode;
  }
  renderModuleSection();
  renderRows();
}

// Periodic refresh: pulls new nodes, callbacks and check results from the back end without touching forms the operator may be editing.
async function syncNodes() {
  try {
    const state = await fetchNodeState();
    if (!state) return;
    applyNodeState(state);
    renderManualNodeSelector();
    renderOpenwrtHosts();
    const oldNode = provisionNode.value;
    renderNodeSelector();
    if (oldNode && nodes.some((n) => n.name === oldNode)) {
      provisionNode.value = oldNode;
    }
  } catch (err) {
    // Keep the table as it is; the monitor poll below still reports reachability.
  }
}

async function refreshMonitor(sendWebhook) {
  monitorStatus.textContent = sendWebhook ? "Refreshing and sending webhook..." : "Refreshing monitoring...";
  await syncNodes();
  renderRows();
  const res = await fetch("/api/nodes/monitor", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ send_webhook: sendWebhook }),
  });
  const data = await res.json();
  if (!res.ok || !data.ok) {
    monitorStatus.textContent = "Failed to refresh monitor.";
    return;
  }

  statusByNode = {};
  for (const node of data.statuses || []) {
    statusByNode[node.name] = node;
  }
  renderRows();

  if (sendWebhook) {
    monitorStatus.textContent = data.webhook.sent
      ? (data.webhook.ok ? `Webhook sent (HTTP ${data.webhook.status}).` : `Webhook failed: ${data.webhook.error}`)
      : "No webhook configured; monitor refreshed.";
  } else {
    monitorStatus.textContent = "Monitoring refreshed.";
  }
}

async function saveConfig() {
  configStatus.textContent = "Saving...";
  const payload = {
    username: document.getElementById("username").value.trim(),
    password: document.getElementById("password").value,
    webhook_url: document.getElementById("webhook_url").value.trim(),
    verify_tls: document.getElementById("verify_tls").value.trim().toLowerCase() !== "false",
  };
  const res = await fetch("/api/nodes/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (!res.ok || !data.ok) {
    configStatus.textContent = "Failed to save config.";
    return;
  }
  configStatus.textContent = `Saved. Password configured: ${data.config.password_set ? "yes" : "no"}`;
}
