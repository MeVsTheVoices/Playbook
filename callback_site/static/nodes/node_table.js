function statusBadge(node) {
  const latest = node.latest_check || {};
  if (!latest.checked_at) return "-";
  return `${latest.status || "unknown"} (${latest.mode || ""}) ${formatTimestamp(latest.checked_at)}`;
}

function nodeDetailSummary(node) {
  const callbackIp = String(node.callback_source_ip || node.address || "").trim();
  const callbackStage = String(node.callback_stage || "").trim();
  const hardware = node.hardware || {};
  const cert = node.cert || {};
  const parts = [];
  if (callbackIp) {
    parts.push(`callback ${callbackIp}`);
  }
  if (callbackStage) {
    parts.push(callbackStage);
  }
  if (hardware.received_at) {
    const hardwareBits = [];
    if (hardware.model) hardwareBits.push(hardware.model);
    if (hardware.source_ip) hardwareBits.push(hardware.source_ip);
    parts.push(`hardware ${hardwareBits.join(" • ") || "present"}`);
  }
  if (cert.cert_present) {
    parts.push(`cert ${cert.cert_subject || cert.cert_fingerprint || "present"}`);
  }
  return parts.join(" | ");
}

function formatTimestamp(raw) {
  const value = String(raw || "").trim();
  if (!value) return "-";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value.replace("T", " ").replace("+00:00", " UTC");
  }
  return parsed.toLocaleString([], {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
  });
}

function callbackLabel(raw) {
  return formatTimestamp(raw);
}

function renderMonitorStats() {
  const total = nodes.length;
  const callbackAddr = nodes.filter((node) => String(node.address_source || "") === "callback").length;
  const recentCallbacks = nodes.filter((node) => String(node.last_callback || "").trim()).length;
  const reachable = Object.values(statusByNode).filter((status) => status && status.reachable === true).length;

  statNodeCountEl.textContent = String(total);
  statReachableCountEl.textContent = String(reachable);
  statCallbackAddressCountEl.textContent = String(callbackAddr);
  statCallbackCountEl.textContent = String(recentCallbacks);
}

function renderRows() {
  rows.innerHTML = "";
  if (!nodes.length) {
    rows.innerHTML = "<tr><td colspan='10'>No active nodes found in inventory groups.</td></tr>";
    renderMonitorStats();
    return;
  }

  for (const node of nodes) {
    const status = statusByNode[node.name] || {};
    const selected = Array.isArray(node.selected_profiles) ? node.selected_profiles : [];
    const selectedHtml = selected.length
      ? selected.map((id) => {
          const p = profileById(id);
          return `<span class='pill'>${p ? p.label : id}</span>`;
        }).join("")
      : "-";
    const reachableClass = status.reachable === true ? "ok" : status.reachable === false ? "bad" : "checking";
    const reachableLabel = status.reachable === true ? "🟢 UP" : status.reachable === false ? "🔴 DOWN" : "🟡 CHECKING";
    const detailSummary = nodeDetailSummary(node);
    const via = status.connect_target && status.connect_target !== node.address
      ? `<div class="note">via ${escapeHtml(status.connect_target)} (${escapeHtml(status.connect_source || "")})</div>`
      : "";

    const tr = document.createElement("tr");
    const removeBtn = node.manual_node
      ? `<button type="button" data-node="${node.name}" data-action="remove" class="secondary">Remove</button>`
      : "";
    tr.innerHTML = `
      <td>${node.name}</td>
      <td>${node.group}</td>
      <td class="mono">${node.address || ""}${via}</td>
      <td>${node.address_source || "inventory"}</td>
      <td><span class="status-chip ${reachableClass}">${reachableLabel}</span></td>
      <td>${status.latency_ms ?? "-"}</td>
      <td>${callbackLabel(status.last_callback || node.last_callback)}</td>
      <td>${selectedHtml}</td>
      <td>${statusBadge(node)}</td>
      <td>
        <div class="note" style="margin-bottom:0.35rem;">${escapeHtml(detailSummary || "No callback details yet.")}</div>
        <div class="inline-actions">
          <button type="button" data-node="${node.name}" data-action="check" class="node-action-btn">Get Hardware Profile</button>
          <button type="button" data-node="${node.name}" data-action="apply" class="node-action-btn">Apply</button>
          <button type="button" data-node="${escapeHtml(node.name)}" data-action="routes" class="node-action-btn secondary">Routes</button>
          ${removeBtn}
        </div>
      </td>
    `;
    rows.appendChild(tr);
  }

  renderMonitorStats();

  rows.querySelectorAll("button[data-node]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const node = btn.getAttribute("data-node") || "";
      const action = btn.getAttribute("data-action") || "check";
      if (action === "routes") {
        NodeRoutes.open(node);
        return;
      }
      if (action === "remove") {
        deleteManualNode(node);
        return;
      }
      runProvision(node, action);
    });
  });
}
