function toPrettyJson(value) {
  try {
    return JSON.stringify(value, null, 2);
  } catch (_) {
    return String(value ?? "");
  }
}

function parsedHardwareProfile(node) {
  const hw = node.hardware || {};
  const cert = node.cert || {};
  const ramMb = Number(hw.ram_mb);
  const ramGb = Number.isFinite(ramMb) ? Math.round((ramMb / 1024) * 10) / 10 : null;
  return {
    node: {
      name: node.name || "",
      group: node.group || "",
      address: node.address || "",
      address_source: node.address_source || "",
      source: node.source || "",
    },
    hardware_profile: {
      model: hw.model || "",
      ram_mb: hw.ram_mb ?? null,
      ram_gb_approx: ramGb,
      sd_capacity_gb: hw.sd_capacity_gb ?? null,
      nvme: hw.nvme ?? null,
      nvme_capacity_gb: hw.nvme_capacity_gb ?? null,
      hat: hw.hat ?? null,
      poe: hw.poe ?? null,
    },
    callback_context: {
      stage: "hardware-check",
      received_at: hw.received_at || "",
      source_ip: hw.source_ip || "",
      callback_profile: node.callback_profile || "",
      callback_instance_id: node.callback_instance_id || "",
      last_callback: node.last_callback || "",
      node_report_id: hw.node_report_id || "",
      node_report_present: Boolean(hw.node_report_id),
    },
    certificate: {
      cert_present: Boolean(cert.cert_present),
      cert_subject: cert.cert_subject || "",
      cert_issuer: cert.cert_issuer || "",
      cert_fingerprint: cert.cert_fingerprint || "",
    },
    latest_check: node.latest_check || {},
  };
}

function rawHardwareView(node) {
  const hw = node.hardware || {};
  return {
    hardware: {
      received_at: hw.received_at || "",
      source_ip: hw.source_ip || "",
      model: hw.model || "",
      ram_mb: hw.ram_mb ?? null,
      sd_capacity_gb: hw.sd_capacity_gb ?? null,
      nvme: hw.nvme ?? null,
      nvme_capacity_gb: hw.nvme_capacity_gb ?? null,
      hat: hw.hat ?? null,
      poe: hw.poe ?? null,
      node_report_id: hw.node_report_id || "",
    },
    node_record: {
      name: node.name || "",
      group: node.group || "",
      address: node.address || "",
      address_source: node.address_source || "",
      source: node.source || "",
      selected_profiles: Array.isArray(node.selected_profiles) ? node.selected_profiles : [],
    },
    latest_check_steps: Array.isArray(node.latest_check_steps) ? node.latest_check_steps : [],
  };
}

function renderExpanderLabel(isOpen, label) {
  return `${isOpen ? "▼" : "▶"} ${label || "node"}`;
}

function latestCheckSummary(node) {
  const latest = node.latest_check || {};
  const steps = Array.isArray(node.latest_check_steps) ? node.latest_check_steps : [];
  const latestStatus = String(latest.status || "").trim().toLowerCase();
  if (latest.ok === false || latestStatus === "failed" || steps.some((step) => String(step.status || "").toLowerCase() === "failed")) {
    return { tone: "failed", label: "Needs Attention" };
  }
  if (latest.ok === true || latestStatus === "ok" || steps.some((step) => ["ok", "completed"].includes(String(step.status || "").toLowerCase()))) {
    return { tone: "ok", label: "Healthy" };
  }
  return { tone: "warn", label: "Unknown" };
}

function renderHardwareKpis() {
  if (!hardwareKpisEl) {
    return;
  }
  const hwNodes = nodes.filter((n) => n.hardware && n.hardware.received_at);
  const nodesWithStorage = hwNodes.filter((node) => {
    const hardware = node.hardware || {};
    return Number(hardware.nvme_capacity_gb || 0) > 0
      || Number(hardware.sd_capacity_gb || 0) > 0
      || Boolean(hardware.nvme);
  }).length;
  const reportLinks = hwNodes.filter((node) => String(node.hardware?.node_report_id || "").trim()).length;
  const certsSeen = hwNodes.filter((node) => Boolean(node.cert?.cert_present)).length;
  const cards = [
    ["Hardware Reports", `${hwNodes.length}/${nodes.length || 0}`],
    ["Nodes With Storage", nodesWithStorage],
    ["Report Links", reportLinks],
    ["Certificates Seen", certsSeen],
  ];
  hardwareKpisEl.innerHTML = "";
  for (const [label, value] of cards) {
    const card = document.createElement("div");
    card.className = "kpi";
    card.innerHTML = `<div class='label'>${escapeHtml(String(label))}</div><div class='value'>${escapeHtml(String(value))}</div>`;
    hardwareKpisEl.appendChild(card);
  }
}

function renderHardwareTable() {
  hwBodyEl.innerHTML = "";
  const hwNodes = nodes.filter((n) => n.hardware && n.hardware.received_at);
  if (!hwNodes.length) {
    hwBodyEl.innerHTML = "<tr><td colspan='6' style='color:var(--muted)'>No hardware-check callbacks received yet. Run <em>Get Hardware Profile</em> in Node Ops to trigger one.</td></tr>";
    return;
  }
  for (const [index, node] of hwNodes.entries()) {
    const hw = node.hardware || {};
    const cert = node.cert || {};
    const storageParts = [];
    const sdCapacity = Number(hw.sd_capacity_gb || 0);
    const nvmeCapacity = Number(hw.nvme_capacity_gb || 0);
    if (sdCapacity > 0) storageParts.push(`${sdCapacity} GB SD`);
    if (nvmeCapacity > 0) storageParts.push(`${nvmeCapacity} GB NVMe`);
    else if (hw.nvme) storageParts.push("NVMe present");
    const storageTotal = sdCapacity + nvmeCapacity;
    const storageValue = storageTotal > 0 ? `${storageTotal} GB total` : "-";
    const storageDetail = storageParts.length ? storageParts.join(" • ") : "No storage data";
    const certTag = cert.cert_present
      ? "<span class='tag ok'>present</span>"
      : "<span class='tag bad'>missing</span>";
    const reportLink = hw.node_report_id
      ? `<a href='/cloud-init/reports/${escapeHtml(hw.node_report_id)}' target='_blank' rel='noopener'>report ↗</a>`
      : "-";
    const hardwareSummary = [
      hw.model || "-",
      hw.ram_mb != null ? `${hw.ram_mb} MB RAM` : "RAM unknown",
      hw.sd_capacity_gb != null ? `${hw.sd_capacity_gb} GB SD` : "SD unknown",
    ].filter(Boolean);
    const hardwareReceivedAt = formatTimestamp(hw.received_at || "-");
    const firstCallbackAt = formatTimestamp(hw.first_callback || "-");
    const summaryRow = document.createElement("tr");
    const detailRowId = `hw-detail-${index}`;
    summaryRow.innerHTML = `
      <td>
        <button type='button' class='expander-btn' aria-expanded='false' data-target='${escapeHtml(detailRowId)}'>
          ${escapeHtml(renderExpanderLabel(false, node.name))}
        </button>
      </td>
      <td class='mono'>${escapeHtml(hw.source_ip || node.address || "-")}</td>
      <td><div class='summary-stack'><div class='summary-main'>${escapeHtml(hardwareSummary[0] || "-")}</div><div class='summary-sub'>${escapeHtml(hardwareSummary.slice(1).join(" • ") || "No summary")}</div></div></td>
      <td>
        <div class='summary-stack'>
          <div class='summary-main' style='font-size:1rem; font-weight:700;'>${escapeHtml(storageValue || "-")}</div>
          <div class='summary-sub'>${escapeHtml(storageDetail || "-")}</div>
        </div>
      </td>
      <td>
        <div class='summary-stack'>
          <div class='summary-main' style='font-size:1rem; font-weight:700;'>${escapeHtml(hardwareReceivedAt || "-")}</div>
          <div class='summary-sub'>First callback: ${escapeHtml(firstCallbackAt || "-")}</div>
        </div>
      </td>
      <td>${reportLink}</td>
    `;
    hwBodyEl.appendChild(summaryRow);

    const detailRow = document.createElement("tr");
    detailRow.className = "hw-detail-row";
    detailRow.id = detailRowId;
    detailRow.hidden = true;
    detailRow.innerHTML = `
      <td colspan='6'>
        <div class='hw-detail-grid'>
          <div class='hw-detail-card'>
            <h4>Node and Callback Summary</h4>
            <dl class='detail-list'>
              <div class='detail-item'><dt>Group</dt><dd>${escapeHtml(node.group || "-")}</dd></div>
              <div class='detail-item'><dt>Address</dt><dd class='mono'>${escapeHtml(hw.source_ip || node.address || "-")}</dd></div>
              <div class='detail-item'><dt>Profiles</dt><dd>${escapeHtml((Array.isArray(node.selected_profiles) ? node.selected_profiles.join(", ") : "") || "-")}</dd></div>
              <div class='detail-item'><dt>Certificate</dt><dd>${certTag}</dd></div>
              <div class='detail-item'><dt>Report</dt><dd>${reportLink}</dd></div>
            </dl>
          </div>
          <div class='hw-detail-card'>
            <h4>Parsed Hardware Profile</h4>
            <pre class='mono hw-pre'>${escapeHtml(toPrettyJson(parsedHardwareProfile(node)))}</pre>
          </div>
          <div class='hw-detail-card'>
            <h4>Raw-ish Callback Data</h4>
            <pre class='mono hw-pre'>${escapeHtml(toPrettyJson(rawHardwareView(node)))}</pre>
          </div>
        </div>
      </td>
    `;
    hwBodyEl.appendChild(detailRow);

    const expander = summaryRow.querySelector(".expander-btn");
    if (expander) {
      expander.addEventListener("click", () => {
        const isOpen = !detailRow.hidden;
        detailRow.hidden = isOpen;
        expander.setAttribute("aria-expanded", isOpen ? "false" : "true");
        expander.textContent = renderExpanderLabel(!isOpen, node.name || "node");
      });
    }
  }
}

function renderFleetKpis() {
  if (!fleetKpisEl) {
    return;
  }
  const hardwareFresh = nodes.filter((n) => n.hardware && n.hardware.received_at).length;
  const certPresent = nodes.filter((n) => n.cert && n.cert.cert_present).length;
  const failedChecks = nodes.filter((n) => n.latest_check && n.latest_check.ok === false).length;
  const cards = [
    ["Nodes Visible", nodes.length],
    ["Hardware Fresh", `${hardwareFresh}/${nodes.length || 0}`],
    ["Certificates Present", `${certPresent}/${nodes.length || 0}`],
    ["Last Check Failed", failedChecks],
  ];
  fleetKpisEl.innerHTML = "";
  for (const [label, value] of cards) {
    const card = document.createElement("div");
    card.className = "kpi";
    card.innerHTML = `<div class='label'>${escapeHtml(label)}</div><div class='value'>${escapeHtml(String(value))}</div>`;
    fleetKpisEl.appendChild(card);
  }
}
