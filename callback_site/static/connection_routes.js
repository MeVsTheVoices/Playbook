window.NodeRoutes = (() => {
  const dialog = document.createElement("dialog");
  dialog.className = "node-routes-dialog";
  dialog.setAttribute("aria-labelledby", "node-routes-title");
  dialog.innerHTML = '<div class="route-dialog-head"><div><p class="route-kicker">Connection paths</p><h2 id="node-routes-title">Node routes</h2></div><form method="dialog"><button aria-label="Close node routes">Close</button></form></div><p class="route-context">Local interface means the dashboard host (the container in Docker). Known paths are not live SSH reachability tests.</p><div class="route-dialog-body" role="status" aria-live="polite"></div>';
  document.body.append(dialog);
  const content = dialog.querySelector(".route-dialog-body");
  let requestId = 0;
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) {
      const bounds = dialog.getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close();
    }
  });
  dialog.addEventListener("close", () => { requestId += 1; });

  function summary(route) {
    if (route.error) return route.error;
    return [
      route.family,
      route.interface ? `local interface ${route.interface}` : "interface not reported",
      route.gateway ? `via ${route.gateway}` : "direct route",
      route.source_ip ? `from ${route.source_ip}` : "",
    ].filter(Boolean).join(" · ");
  }

  async function open(nodeName, target = "") {
    const id = ++requestId;
    dialog.querySelector("h2").textContent = `Routes to ${nodeName}`;
    content.textContent = "Looking up known connection paths...";
    if (!dialog.open) dialog.showModal();
    try {
      const response = await fetch(`/api/nodes/${encodeURIComponent(nodeName)}/connection-profiles?routes=1`);
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || "Could not load routes.");
      if (id !== requestId) return;
      content.replaceChildren();
      const strategy = document.createElement("p");
      strategy.className = "route-context";
      strategy.textContent = `Node Ops strategy: ${data.strategy}. Terminal uses the target in its form, which may differ.`;
      content.append(strategy);
      for (const candidate of data.candidates) {
        const card = document.createElement("article");
        card.className = "route-path";
        const title = document.createElement("strong");
        title.textContent = candidate.address;
        const badge = document.createElement("span");
        badge.className = "route-tag";
        badge.textContent = target === candidate.address ? "Terminal target" : candidate.id === data.selected ? "Preferred by Node Ops" : "Alternative";
        const type = document.createElement("p");
        const isFqdn = candidate.kind === "tailnet_fqdn" || (candidate.sources || []).includes("tailscale_fqdn");
        const label = isFqdn ? "Tailnet FQDN (MagicDNS)" : candidate.tailnet ? "Tailnet" : "LAN / hostname";
        const sources = (candidate.sources || [candidate.source]).map((source) => source.replaceAll("_", " ")).join(", ");
        type.textContent = `${label} · ${sources} · SSH port ${candidate.port} · ${candidate.usable ? "candidate" : "unverified / transit"}`;
        const route = document.createElement("p");
        route.className = "route-context";
        route.textContent = summary(candidate.route);
        card.append(title, badge, type, route);
        if (isFqdn) {
          const certificate = document.createElement("p");
          certificate.className = "route-context";
          certificate.textContent = `Certificate hostname: ${candidate.fqdn}. HTTPS certificates are not configured by this view.`;
          card.append(certificate);
        }
        content.append(card);
      }
      if (data.candidates.some((candidate) => candidate.tailnet) && !data.candidates.some((candidate) => candidate.kind === "tailnet_fqdn" || (candidate.sources || []).includes("tailscale_fqdn"))) {
        const notice = document.createElement("p");
        notice.className = "route-context";
        notice.textContent = "Tailscale FQDN not discovered. The dashboard needs Tailscale peer status or DNS that resolves the node's Tailscale IP to its MagicDNS name.";
        content.append(notice);
      }
      if (!data.candidates.length) content.append("No connection paths recorded for this node yet.");
    } catch (error) {
      if (id === requestId) content.textContent = `Route lookup failed: ${error.message}`;
    }
  }
  return { open, summary };
})();
