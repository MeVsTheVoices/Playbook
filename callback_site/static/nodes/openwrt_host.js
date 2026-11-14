// Router tools live in Module Configuration and only show while an openwrt-group node is selected.
function selectedOpenwrtHost() {
  const node = nodes.find((item) => item.name === provisionNode.value);
  return node && String(node.group || "").toLowerCase() === "openwrt" ? node : null;
}

let openwrtShownFor = "";

function renderOpenwrtHosts() {
  const section = document.getElementById("openwrt-host");
  if (!section) return;
  const host = selectedOpenwrtHost();
  section.hidden = !host;
  const name = host ? host.name : "";
  if (name !== openwrtShownFor) {
    openwrtShownFor = name;
    document.getElementById("openwrt-status").textContent = "";
    document.getElementById("openwrt-password").value = "";
    const facts = document.getElementById("openwrt-uci-facts");
    facts.hidden = true;
    facts.innerHTML = "";
  }
}

async function callOpenwrt(url, payload) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  try {
    return await res.json();
  } catch (err) {
    return { ok: false, error: `Server did not return JSON (${res.status}).` };
  }
}

function openwrtPort() {
  return Number(document.getElementById("openwrt-port").value || 22);
}

function openwrtConnection(host) {
  return {
    host: String(host.address || "").trim(),
    port: openwrtPort(),
    username: String(host.node_username || "root").trim() || "root",
    password: document.getElementById("openwrt-password").value,
  };
}

async function probeOpenwrtHost() {
  const statusEl = document.getElementById("openwrt-status");
  const host = selectedOpenwrtHost();
  if (!host) return;
  statusEl.textContent = `Probing ${host.name}...`;
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
  statusEl.textContent = probe.ok
    ? `SSH login OK as ${data.request?.username || "root"}@${data.request?.host || host.address}:${data.request?.port || openwrtPort()}${host.certificate ? ` with key ${host.certificate}` : ""}.`
    : `SSH login failed: ${probe.error || "unknown error"}${probe.details ? ` (${probe.details.split("\n").pop()})` : ""}`;
}

async function pushOpenwrtKey() {
  const statusEl = document.getElementById("openwrt-status");
  const host = selectedOpenwrtHost();
  if (!host) return;
  if (!host.certificate) {
    statusEl.textContent = `${host.name} has no certificate / SSH key saved; edit the node and pick one.`;
    return;
  }
  statusEl.textContent = `Pushing ${host.certificate} to ${host.name}...`;
  const data = await callOpenwrt("/api/openwrt/push-key", { ...openwrtConnection(host), key_name: host.certificate });
  statusEl.textContent = data.ok && data.pushed
    ? `Key ${data.key_name} is authorized on ${host.name}.`
    : (data.error || "Key push failed.");
}

async function readOpenwrtUci() {
  const statusEl = document.getElementById("openwrt-status");
  const factsEl = document.getElementById("openwrt-uci-facts");
  const host = selectedOpenwrtHost();
  if (!host) return;
  statusEl.textContent = `Reading UCI from ${host.name}...`;
  const data = await callOpenwrt("/api/openwrt/uci", { node: host.name, port: openwrtPort() });
  if (host.name !== openwrtShownFor) return;
  const facts = Array.isArray(data.facts) ? data.facts : [];
  factsEl.innerHTML = "";
  for (const fact of facts) {
    const dt = document.createElement("dt");
    dt.textContent = fact.key;
    const dd = document.createElement("dd");
    dd.textContent = fact.value;
    factsEl.append(dt, dd);
  }
  factsEl.hidden = !facts.length;
  statusEl.textContent = data.ok
    ? `UCI read from ${data.target} (${facts.length} values; wireless keys are never read).`
    : (data.error || "UCI read failed.");
}
