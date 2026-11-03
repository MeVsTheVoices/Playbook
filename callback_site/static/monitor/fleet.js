function renderCloudInitCoverage() {
  if (!monitorCloudInitBodyEl || !monitorOptionCatalogEl) {
    return;
  }
  monitorCloudInitBodyEl.innerHTML = "";
  const rows = [...nodes].sort((a, b) => String(a.name || "").localeCompare(String(b.name || "")));
  const optionCatalog = Array.isArray(sourceOfTruthState?.monitor?.cloud_init_options)
    ? sourceOfTruthState.monitor.cloud_init_options
    : [];
  const assignedNodes = rows.filter((node) => Array.isArray(node.cloud_init_option_ids) && node.cloud_init_option_ids.length > 0).length;
  const totalAssignments = rows.reduce((sum, node) => {
    const optionIds = Array.isArray(node.cloud_init_option_ids) ? node.cloud_init_option_ids : [];
    return sum + optionIds.length;
  }, 0);

  if (cloudInitCoverageKpisEl) {
    const cards = [
      ["Nodes With Assignments", assignedNodes],
      ["Nodes Without Assignments", Math.max(0, rows.length - assignedNodes)],
      ["Cataloged Options", optionCatalog.length],
      ["Total Active Assignments", totalAssignments],
    ];
    cloudInitCoverageKpisEl.innerHTML = "";
    for (const [label, value] of cards) {
      const card = document.createElement("div");
      card.className = "kpi";
      card.innerHTML = `<div class='label'>${escapeHtml(String(label))}</div><div class='value'>${escapeHtml(String(value))}</div>`;
      cloudInitCoverageKpisEl.appendChild(card);
    }
  }

  if (!rows.length) {
    monitorCloudInitBodyEl.innerHTML = "<tr><td colspan='4' style='color:var(--muted)'>No nodes available.</td></tr>";
  }
  for (const node of rows) {
    const optionIds = Array.isArray(node.cloud_init_option_ids) ? node.cloud_init_option_ids : [];
    const profiles = Array.isArray(node.selected_profiles) ? node.selected_profiles : [];
    const optionSummary = optionIds.length
      ? optionIds.map((id) => `<span class='tag'>${escapeHtml(id)}</span>`).join(" ")
      : "<span class='tag'>none</span>";
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><strong>${escapeHtml(node.name || "-")}</strong></td>
      <td>${optionSummary}</td>
      <td>${escapeHtml(String(optionIds.length))}</td>
      <td>${escapeHtml(profiles.join(", ") || "-")}</td>
    `;
    monitorCloudInitBodyEl.appendChild(tr);
  }

  monitorOptionCatalogEl.innerHTML = "";
  if (!optionCatalog.length) {
    monitorOptionCatalogEl.innerHTML = "<tr><td colspan='3' style='color:var(--muted)'>No cloud-init options cataloged in monitor state.</td></tr>";
    return;
  }
  for (const option of optionCatalog) {
    const optionKey = String(option.id || option.name || "").trim();
    const usedByCount = rows.filter((node) => {
      const optionIds = Array.isArray(node.cloud_init_option_ids) ? node.cloud_init_option_ids : [];
      return optionKey && optionIds.includes(optionKey);
    }).length;
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class='mono'>${escapeHtml(optionKey || "-")}</td>
      <td>${escapeHtml(option.label || option.description || "-")}</td>
      <td>${escapeHtml(String(usedByCount))}</td>
    `;
    monitorOptionCatalogEl.appendChild(tr);
  }
}

function renderLatestChecks() {
  if (!latestChecksBodyEl) {
    return;
  }
  latestChecksBodyEl.innerHTML = "";
  const rows = [];
  for (const node of nodes) {
    const latest = node.latest_check || {};
    const checkedAt = formatTimestamp(latest.checked_at || "-");
    const steps = Array.isArray(node.latest_check_steps) ? node.latest_check_steps : [];
    if (!steps.length) {
      rows.push({
        node: node.name || "-",
        checkedAt,
        step: "-",
        status: latest.ok === true ? "ok" : latest.ok === false ? "failed" : "unknown",
        output: "No step details available",
      });
      continue;
    }
    for (const step of steps) {
      rows.push({
        node: node.name || "-",
        checkedAt,
        step: step.profile_id || "-",
        status: step.status || "unknown",
        output: step.output || "-",
      });
    }
  }

  if (!rows.length) {
    latestChecksBodyEl.innerHTML = "<tr><td colspan='5' style='color:var(--muted)'>No provision check outcomes yet.</td></tr>";
    if (latestCheckKpisEl) {
      latestCheckKpisEl.innerHTML = "";
    }
    return;
  }

  if (latestCheckKpisEl) {
    const nodesWithChecks = nodes.filter((node) => node.latest_check && node.latest_check.checked_at).length;
    const healthy = nodes.filter((node) => latestCheckSummary(node).tone === "ok").length;
    const failing = nodes.filter((node) => latestCheckSummary(node).tone === "failed").length;
    const cards = [
      ["Nodes With Checks", `${nodesWithChecks}/${nodes.length || 0}`],
      ["Healthy", healthy],
      ["Needs Attention", failing],
      ["Step Results Shown", rows.length],
    ];
    latestCheckKpisEl.innerHTML = "";
    for (const [label, value] of cards) {
      const card = document.createElement("div");
      card.className = "kpi";
      card.innerHTML = `<div class='label'>${escapeHtml(String(label))}</div><div class='value'>${escapeHtml(String(value))}</div>`;
      latestCheckKpisEl.appendChild(card);
    }
  }

  for (const row of rows.slice(0, 120)) {
    const tone = ["ok", "completed"].includes(String(row.status || "").toLowerCase())
      ? "ok"
      : String(row.status || "").toLowerCase() === "failed"
        ? "failed"
        : "warn";
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><strong>${escapeHtml(row.node)}</strong></td>
      <td>${escapeHtml(row.checkedAt)}</td>
      <td class='mono'>${escapeHtml(row.step)}</td>
      <td><span class='status-tag ${tone}'>${escapeHtml(row.status)}</span></td>
      <td class='mono'>${escapeHtml(String(row.output || "-").slice(0, 240))}</td>
    `;
    latestChecksBodyEl.appendChild(tr);
  }
}
