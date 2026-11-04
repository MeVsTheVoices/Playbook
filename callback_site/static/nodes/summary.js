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

async function loadSummary() {
  const sotRes = await fetch("/api/state/source-of-truth");
  const sotData = await sotRes.json();
  if (!sotRes.ok || !sotData.ok) {
    monitorStatus.textContent = "Failed to load node summary.";
    return;
  }

  const state = sotData.state || {};
  nodes = Array.isArray(state.nodes) ? state.nodes : [];
  profiles = Array.isArray(state.profiles) ? state.profiles : [];
  nodeInputs = (state.provisioning && typeof state.provisioning.node_inputs === "object") ? state.provisioning.node_inputs : {};
  renderProfileSegmentOptions();
  manualNodes = (state.provisioning && typeof state.provisioning.manual_nodes === "object") ? state.provisioning.manual_nodes : {};
  renderManualNodeSelector();
  renderOpenwrtHosts();

  statusByNode = {};
  for (const node of nodes) {
    statusByNode[node.name] = {
      name: node.name,
      address: node.address,
      address_source: node.address_source || "inventory",
      reachable: null,
      latency_ms: null,
      last_callback: node.last_callback || "",
      error: "",
    };
  }

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

async function refreshMonitor(sendWebhook) {
  monitorStatus.textContent = sendWebhook ? "Refreshing and sending webhook..." : "Refreshing monitoring...";
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
