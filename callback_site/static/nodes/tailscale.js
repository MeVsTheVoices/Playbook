window.NodeTailscale = (() => {
  const dialog = document.createElement("dialog");
  dialog.className = "node-routes-dialog";
  dialog.setAttribute("aria-labelledby", "node-tailscale-title");
  dialog.innerHTML = '<div class="route-dialog-head"><div><p class="route-kicker">Live node perspective</p><h2 id="node-tailscale-title">Tailscale</h2></div><button type="button" data-refresh>Ask node again</button><form method="dialog"><button>Close</button></form></div><p class="route-context">Read-only SSH query: tailscale status --json on this node, not the dashboard host. Visible peers reflect its network map and policy; online does not prove end-to-end reachability. No connectivity probes are sent.</p><div class="route-dialog-body" role="status" aria-live="polite"></div>';
  document.body.append(dialog);
  const content = dialog.querySelector(".route-dialog-body");
  const refresh = dialog.querySelector("[data-refresh]");
  let node = "";
  let requestId = 0;
  dialog.addEventListener("close", () => { requestId += 1; });
  refresh.addEventListener("click", () => open(node));

  function paragraph(parent, text, className = "") {
    const element = document.createElement("p");
    element.className = className;
    element.textContent = text;
    parent.append(element);
  }

  function peerCard(peer, title) {
    const card = document.createElement("article");
    card.className = "route-path";
    const heading = document.createElement("strong");
    heading.textContent = `${title}: ${peer.DNSName || peer.HostName || peer.ID || "Not reported"}`;
    card.append(heading);
    paragraph(card, [
      (peer.TailscaleIPs || []).join(", "),
      peer.OS,
      peer.Online === true ? "Online" : peer.Online === false ? "Offline" : "Online state not reported",
      peer.Active === true ? "Active traffic" : "",
      peer.CurAddr ? `Direct endpoint: ${peer.CurAddr}` : "",
      peer.Relay ? `DERP region: ${peer.Relay}` : "",
      peer.PeerRelay ? `Peer relay: ${peer.PeerRelay}` : "",
      peer.ExitNode ? "Selected exit node" : peer.ExitNodeOption ? "Exit node available" : "",
      peer.LastSeen ? `Last seen: ${peer.LastSeen}` : "",
    ].filter(Boolean).join(" | "));
    return card;
  }

  async function open(nodeName) {
    node = nodeName;
    const id = ++requestId;
    dialog.querySelector("h2").textContent = `Tailscale on ${nodeName}`;
    content.textContent = "Asking this node for its current Tailscale view...";
    refresh.disabled = true;
    if (!dialog.open) dialog.showModal();
    try {
      const response = await fetch(`/api/nodes/tailscale?node=${encodeURIComponent(nodeName)}`, { cache: "no-store" });
      const data = await response.json();
      if (id !== requestId) return;
      if (!response.ok || !data.ok) throw new Error(data.error || "Could not read Tailscale status.");
      const status = data.status;
      content.replaceChildren();
      paragraph(content, `Read at ${data.read_at} via ${data.target}`, "route-context");
      paragraph(content, `State: ${status.BackendState} | Version: ${status.Version || "not reported"} | Tailnet: ${(status.CurrentTailnet || {}).Name || status.MagicDNSSuffix || "not reported"}`);
      paragraph(content, (status.Health || []).length ? `Health warnings: ${status.Health.join("; ")}` : "No health warnings reported.");
      content.append(peerCard(status.Self, "This node"));
      paragraph(content, `${status.Peer.length} visible peer(s). DERP region is the peer's home relay, not proof traffic is relayed. Direct endpoints and counters describe observed traffic, not a fresh connectivity test.`);
      for (const peer of status.Peer) content.append(peerCard(peer, "Peer"));
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = "Status details (filtered JSON)";
      const raw = document.createElement("pre");
      raw.className = "log";
      raw.textContent = JSON.stringify(status, null, 2);
      details.append(summary, raw);
      content.append(details);
    } catch (error) {
      if (id === requestId) content.textContent = `Tailscale query failed: ${error.message}`;
    } finally {
      if (id === requestId) refresh.disabled = false;
    }
  }
  return { open };
})();
