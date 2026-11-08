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
        type.textContent = `${candidate.tailnet ? "Tailnet" : "LAN / hostname"} · ${candidate.kind.replaceAll("_", " ")} · SSH port ${candidate.port} · ${candidate.usable ? "candidate" : "unverified / transit"}`;
        const route = document.createElement("p");
        route.className = "route-context";
        route.textContent = summary(candidate.route);
        card.append(title, badge, type, route);
        content.append(card);
      }
      if (!data.candidates.length) content.append("No connection paths recorded for this node yet.");
    } catch (error) {
      if (id === requestId) content.textContent = `Route lookup failed: ${error.message}`;
    }
  }
  return { open, summary };
})();
