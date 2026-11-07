function openwrtHostNodes() {
  return nodes.filter((node) => String(node?.group || "").toLowerCase() === "openwrt");
}

function selectedOpenwrtHost() {
  const name = document.getElementById("openwrt-host-select").value;
  return openwrtHostNodes().find((node) => node.name === name) || null;
}

function renderOpenwrtHosts() {
  const select = document.getElementById("openwrt-host-select");
  const summary = document.getElementById("openwrt-host-summary");
  if (!select || !summary) return;
  const current = select.value;
  const hosts = openwrtHostNodes().sort((a, b) => String(a.name).localeCompare(String(b.name)));
  select.innerHTML = "";
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = hosts.length ? "Select a router..." : "No OpenWrt hosts yet";
  select.appendChild(placeholder);
  for (const host of hosts) {
    const opt = document.createElement("option");
    opt.value = host.name;
    opt.textContent = host.address ? `${host.name} (${host.address})` : host.name;
    select.appendChild(opt);
  }
  if (current && hosts.some((host) => host.name === current)) {
    select.value = current;
  }
  summary.textContent = hosts.length
    ? `${hosts.length} router host${hosts.length === 1 ? "" : "s"}: ${hosts.map((host) => host.name).join(", ")}`
    : "No router hosts yet";
}

function addOpenwrtHost() {
  const card = document.getElementById("access-manual-node");
  if (card) card.open = true;
  manualNodeSelectorEl.value = "";
  resetManualNodeForm();
  manualNodeNameEl.value = "openwrt-router";
  manualNodeGroupEl.value = "openwrt";
  manualNodeUsernameEl.value = "root";
  manualNodeShellEl.value = "/bin/ash";
  manualNodeStatus.textContent = "OpenWrt host template loaded: set the router IP and certificate, then use Add / Update Manual Node.";
  manualNodeAddressEl.scrollIntoView({ behavior: "smooth", block: "center" });
  manualNodeAddressEl.focus();
}

async function callOpenwrt(url, payload) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  let data = null;
  try {
    data = await res.json();
  } catch (err) {
    data = { ok: false, error: `Server did not return JSON (${res.status}).` };
  }
  return data;
}

function openwrtConnection(host) {
  return {
    host: String(host.address || "").trim(),
    port: Number(document.getElementById("openwrt-port").value || 22),
    username: String(host.node_username || "root").trim() || "root",
    password: document.getElementById("openwrt-password").value,
  };
}

async function probeOpenwrtHost() {
  const statusEl = document.getElementById("openwrt-status");
  const resultEl = document.getElementById("openwrt-result");
  const host = selectedOpenwrtHost();
  if (!host) {
    statusEl.textContent = "Select an OpenWrt host first.";
    return;
  }
  statusEl.textContent = `Probing ${host.name}...`;
  resultEl.hidden = true;
  const data = await callOpenwrt("/api/openwrt/inspect", {
    ...openwrtConnection(host),
    connection: "ssh",
    module_hint: "opkg",
    become: false,
    ssh_key_name: host.certificate || "",
  });
  if (!data.ok) {
    statusEl.textContent = data.error || "Probe failed.";
    return;
  }
  const probe = data.probe || {};
  const stats = data.stats || {};
  statusEl.textContent = `${host.name}: ${probe.ok ? "connected" : "connection failed"} | module ${data.module || "?"} | ${stats.summary || ""}`;
  resultEl.textContent = [probe.error, probe.details, "", data.inventory_preview].filter((part) => part !== undefined && part !== null).join("\n").trim();
  resultEl.hidden = false;
}

async function pushOpenwrtKey() {
  const statusEl = document.getElementById("openwrt-status");
  const host = selectedOpenwrtHost();
  if (!host) {
    statusEl.textContent = "Select an OpenWrt host first.";
    return;
  }
  if (!host.certificate) {
    statusEl.textContent = `${host.name} has no certificate / SSH key saved; edit the host and pick one.`;
    return;
  }
  statusEl.textContent = `Pushing ${host.certificate} to ${host.name}...`;
  const data = await callOpenwrt("/api/openwrt/push-key", { ...openwrtConnection(host), key_name: host.certificate });
  statusEl.textContent = data.ok
    ? `Key ${data.key_name} pushed to ${host.name}. ${data.details || ""}`.trim()
    : (data.error || "Key push failed.");
}
