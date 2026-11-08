function fillNodeForm(nodeName) {
  const node = nodes.find((n) => n.name === nodeName);
  if (!node) return;
  document.getElementById("node-name").value = node.name || "";
  document.getElementById("node-group").value = node.group || "";
  document.getElementById("node-address").value = node.address || "";
  document.getElementById("node-enabled").value = node.enabled ? "true" : "false";
  document.getElementById("node-notes").value = node.notes || "";
  nodeFormStatusEl.textContent = `Editing ${node.name}.`;
}

function clearNodeForm() {
  document.getElementById("node-name").value = "";
  document.getElementById("node-group").value = "";
  document.getElementById("node-address").value = "";
  document.getElementById("node-enabled").value = "true";
  document.getElementById("node-notes").value = "";
  nodeFormStatusEl.textContent = "";
}

function renderSourceOfTruth() {
  if (!sourceOfTruthState || typeof sourceOfTruthState !== "object") {
    sotBodyEl.innerHTML = "<tr><td colspan='5'>Source-of-truth payload is unavailable.</td></tr>";
    sotMetaEl.textContent = "Unified state endpoint did not return data.";
    return;
  }

  const counts = sourceOfTruthState.counts || {};
  const kpis = [
    ["Nodes In Monitor", counts.nodes_in_monitor],
    ["Nodes With Hardware", counts.nodes_with_hardware],
    ["Catalog Available", `${counts.catalog_available || 0} / ${counts.catalog_entries || 0}`],
    ["Image History Records", counts.image_history_records],
  ];
  sotKpisEl.innerHTML = "";
  for (const [label, value] of kpis) {
    const card = document.createElement("div");
    card.className = "kpi";
    card.innerHTML = `<div class='label'>${escapeHtml(label)}</div><div class='value'>${escapeHtml(value == null ? "-" : String(value))}</div>`;
    sotKpisEl.appendChild(card);
  }

  const rows = Array.isArray(sourceOfTruthState.nodes) ? sourceOfTruthState.nodes : [];
  sotBodyEl.innerHTML = "";
  if (!rows.length) {
    sotBodyEl.innerHTML = "<tr><td colspan='5' style='color:var(--muted)'>No nodes in unified state yet.</td></tr>";
  }
  for (const [index, row] of rows.slice(0, 14).entries()) {
    const image = row.image || {};
    const provisionHow = Array.isArray(row.provision_how) ? row.provision_how : [];
    const provisionText = provisionHow.map((item) => item.label || item.id || "-").join(", ") || "-";
    const latestCheck = row.latest_check || {};
    const latestCheckTs = formatTimestamp(latestCheck.checked_at || "-");
    const latestCheckOk = latestCheck.ok === true ? "ok" : latestCheck.ok === false ? "failed" : "-";
    const sourceText = compactPathLabel(image.source_value || image.source || "-");
    const cached = image.local_available ? "yes" : "no";
    const checkTone = latestCheck.ok === true ? "ok" : latestCheck.ok === false ? "failed" : "warn";
    const detailRowId = `sot-detail-${index}`;

    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><strong>${escapeHtml(row.name || "-")}</strong></td>
      <td class='mono'>${escapeHtml(row.address || "-")}</td>
      <td>
        <div class='token-list'>
          <button type='button' class='expander-btn' aria-expanded='false' data-target='${escapeHtml(detailRowId)}'>${escapeHtml(renderExpanderLabel(false, row.name || "node"))}</button>
          <span class='summary-sub'>${escapeHtml(provisionHow.length ? `${provisionHow.length} profile${provisionHow.length === 1 ? "" : "s"}` : "No profiles")}</span>
        </div>
      </td>
      <td><span class='status-tag ${checkTone}'>${escapeHtml(`${latestCheckOk} @ ${latestCheckTs}`)}</span></td>
      <td><div class='summary-stack'><div class='summary-main mono'>${escapeHtml(sourceText)}</div><div class='summary-sub'>cached: ${escapeHtml(cached)}</div></div></td>
    `;
    sotBodyEl.appendChild(tr);

    const detailRow = document.createElement("tr");
    detailRow.className = "sot-detail-row";
    detailRow.id = detailRowId;
    detailRow.hidden = true;
    detailRow.innerHTML = `
      <td colspan='5'>
        <div class='sot-detail-grid'>
          <div class='sot-detail-card'>
            <h4>Provisioning Summary</h4>
            <dl class='detail-list'>
              <div class='detail-item'><dt>Profiles</dt><dd>${escapeHtml(provisionText)}</dd></div>
              <div class='detail-item'><dt>Group</dt><dd>${escapeHtml(row.group || "-")}</dd></div>
              <div class='detail-item'><dt>Address Source</dt><dd>${escapeHtml(row.address_source || "-")}</dd></div>
              <div class='detail-item'><dt>Source</dt><dd>${escapeHtml(row.source || "-")}</dd></div>
              <div class='detail-item'><dt>Selected</dt><dd>${escapeHtml((Array.isArray(row.selected_profiles) ? row.selected_profiles.join(", ") : "") || "-")}</dd></div>
            </dl>
          </div>
          <div class='sot-detail-card'>
            <h4>Image and Check State</h4>
            <dl class='detail-list'>
              <div class='detail-item'><dt>Image Source</dt><dd class='mono'>${escapeHtml(image.source_value || image.source || "-")}</dd></div>
              <div class='detail-item'><dt>Cached</dt><dd>${escapeHtml(cached)}</dd></div>
              <div class='detail-item'><dt>Hash Status</dt><dd>${escapeHtml(String(image.hash_status || "-") || "-")}</dd></div>
              <div class='detail-item'><dt>Job Status</dt><dd>${escapeHtml(String(image.job_status || "-") || "-")}</dd></div>
              <div class='detail-item'><dt>Latest Check</dt><dd>${escapeHtml(`${latestCheckOk} @ ${latestCheckTs}`)}</dd></div>
            </dl>
          </div>
        </div>
      </td>
    `;
    sotBodyEl.appendChild(detailRow);

    const expander = tr.querySelector(".expander-btn");
    if (expander) {
      expander.addEventListener("click", () => {
        const isOpen = !detailRow.hidden;
        detailRow.hidden = isOpen;
        expander.setAttribute("aria-expanded", isOpen ? "false" : "true");
        expander.textContent = renderExpanderLabel(!isOpen, row.name || "node");
      });
    }
  }

  const generatedAt = sourceOfTruthState.generated_at || "unknown";
  sotMetaEl.textContent = `Generated ${generatedAt}. Source files: callbacks + monitor_state + node_provisioning + image_history + image_catalog + inventory.`;
}

async function loadSummary() {
  monitorStatus.textContent = "Loading...";
  const sotRes = await fetch("/api/state/source-of-truth");
  const sotPayload = await sotRes.json();
  if (!sotRes.ok || !sotPayload.ok) {
    monitorStatus.textContent = sotPayload.error || "Failed to load monitor summary.";
    return;
  }
  sourceOfTruthState = sotPayload.state || null;
  const allNodes = Array.isArray(sourceOfTruthState && sourceOfTruthState.nodes) ? sourceOfTruthState.nodes : [];
  nodes = allNodes.filter((node) => node && node.in_monitor_view);
  renderHardwareTable();
  renderHardwareKpis();
  renderNodeSelectors();
  renderMonitorNodeSelector();
  renderFleetKpis();
  renderSourceOfTruth();
  renderCloudInitCoverage();
  renderLatestChecks();
  const hwCount = nodes.filter((n) => n.hardware && n.hardware.received_at).length;
  monitorStatus.textContent = `${nodes.length} node${nodes.length === 1 ? "" : "s"} loaded, ${hwCount} with hardware data.`;
}

async function fetchJsonWithTimeout(url, options = {}, timeoutMs = 20000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}
