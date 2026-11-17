let networkRequestId = 0;

function resetNodeNetwork() {
  networkRequestId += 1;
  document.getElementById("node-network-read").disabled = false;
  document.getElementById("node-network-status").textContent = "Select a node and read its network facts.";
  document.getElementById("node-network-result").replaceChildren();
}

async function readNodeNetwork() {
  const node = provisionNode.value;
  const status = document.getElementById("node-network-status");
  const output = document.getElementById("node-network-result");
  const button = document.getElementById("node-network-read");
  const requestId = ++networkRequestId;
  output.replaceChildren();
  if (!node) {
    status.textContent = "Select a node first.";
    return;
  }
  button.disabled = true;
  status.textContent = `Reading network facts from ${node}...`;
  try {
    const response = await fetchJsonWithTimeout(`/api/nodes/network?node=${encodeURIComponent(node)}`, {cache: "no-store"}, 160000);
    const data = await response.json();
    if (requestId !== networkRequestId) return;
    if (!response.ok || !data.ok) throw new Error(data.error || `HTTP ${response.status}`);
    const network = data.network;
    const summary = document.createElement("p");
    const route = network.default_ipv4 || {};
    summary.textContent = `Default IPv4: ${route.interface || "not reported"} · ${route.address || "not reported"} · Gateway: ${route.gateway || "not reported"} · DNS: ${(network.dns_servers || []).join(", ") || "not reported"}`;
    output.appendChild(summary);
    for (const iface of network.interfaces || []) {
      const card = document.createElement("div");
      const heading = document.createElement("strong");
      heading.textContent = iface.name;
      const details = document.createElement("p");
      const ipv4 = iface.ipv4 || {};
      const ipv6 = (iface.ipv6 || []).map((item) => `${item.address}${item.prefix != null ? `/${item.prefix}` : ""}`).join(", ");
      details.textContent = [
        iface.type ? `Type: ${iface.type}` : "",
        iface.active == null ? "" : `State: ${iface.active ? "up" : "down"}`,
        iface.mac ? `MAC: ${iface.mac}` : "",
        iface.mtu != null ? `MTU: ${iface.mtu}` : "",
        ipv4.address ? `IPv4: ${ipv4.address}${ipv4.prefix != null ? `/${ipv4.prefix}` : ""}` : "",
        ipv6 ? `IPv6: ${ipv6}` : "",
        iface.ssid ? `SSID: ${iface.ssid}` : "",
      ].filter(Boolean).join(" · ") || "No details reported";
      card.append(heading, details);
      output.appendChild(card);
    }
    if (!network.interfaces.length) output.appendChild(document.createTextNode("No interfaces reported."));
    status.textContent = `Read from ${data.target} at ${data.read_at}.`;
  } catch (error) {
    if (requestId !== networkRequestId) return;
    status.textContent = `Could not read ${node}: ${error.message || error}`;
  } finally {
    if (requestId === networkRequestId) button.disabled = false;
  }
}

document.getElementById("node-network-read").addEventListener("click", readNodeNetwork);
