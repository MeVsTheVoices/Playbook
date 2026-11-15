(() => {
  const element = (name) => document.getElementById(`tailnet-api-${name}`);
  const statusList = element("status");
  if (!statusList) return;
  let busy = false;
  let installed = false;
  let openedFor = null;
  const panels = [element("live"), element("setup")];

  // Only one panel open at a time; native via the shared details name, this covers older browsers.
  panels.forEach((panel) => panel.addEventListener("toggle", () => {
    if (panel.open) panels.forEach((other) => { if (other !== panel) other.open = false; });
  }));

  // Live checks when they come back, otherwise setup; re-chosen only when that answer changes,
  // so the 30-second refresh never undoes a panel the user opened.
  function choosePanel(hasLive) {
    if (openedFor === hasLive) return;
    openedFor = hasLive;
    (hasLive ? element("live") : element("setup")).open = true;
  }

  function row(label, value) {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    if (value instanceof Node) dd.append(value);
    else dd.textContent = value;
    return [dt, dd];
  }

  function code(text) {
    const el = document.createElement("code");
    el.textContent = text;
    return el;
  }

  function since(iso) {
    const seconds = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
    if (!Number.isFinite(seconds)) return "";
    if (seconds < 120) return `${Math.round(seconds)}s`;
    if (seconds < 7200) return `${Math.round(seconds / 60)}m`;
    if (seconds < 172800) return `${Math.round(seconds / 3600)}h`;
    return `${Math.round(seconds / 86400)}d`;
  }

  function check(state, label, detail) {
    const item = document.createElement("li");
    const chip = document.createElement("span");
    chip.className = `chip ${{ ok: "completed", warn: "running", bad: "failed" }[state] || ""}`;
    chip.textContent = { ok: "OK", warn: "Wait", bad: "Fail" }[state] || "Info";
    const name = document.createElement("strong");
    name.textContent = label;
    const text = document.createElement("span");
    text.textContent = detail;
    item.append(chip, name, text);
    return item;
  }

  function renderLive(live, hostname, reason) {
    choosePanel(!!live);
    if (!live) {
      element("verdict").textContent = reason;
      element("verdict").className = "tailnet-api-verdict";
      element("checked").textContent = "";
      element("checks").replaceChildren();
      return;
    }
    const verdict = element("verdict");
    verdict.textContent = live.verdict;
    verdict.className = `tailnet-api-verdict chip ${live.healthy ? "completed" : "failed"}`;
    element("checked").textContent = live.checked_at ? `· checked ${new Date(live.checked_at).toLocaleTimeString()}` : "";
    element("try").textContent = `tailscale configure kubeconfig ${hostname || "<hostname>"}`;
    const { pod = {}, tailnet = {}, logs = {}, network = {} } = live;
    const items = [];
    items.push(pod.found
      ? check(pod.ready ? "ok" : "bad", "Pod", `${pod.name} on ${pod.node || "?"} · ${pod.phase}${pod.started_at ? ` · up ${since(pod.started_at)}` : ""} · ${pod.restarts} restart${pod.restarts === 1 ? "" : "s"}`)
      : check("bad", "Pod", "no operator pod found"));
    items.push(tailnet.logged_in
      ? check("ok", "Tailnet", `joined as ${tailnet.name} (node ${tailnet.node_id})`)
      : check("bad", "Tailnet", "not logged in; check the OAuth client and tag:k8s-operator"));
    items.push(check(tailnet.certificate ? "ok" : "warn", "TLS certificate",
      tailnet.certificate ? "issued" : "not issued yet; it is fetched on the first kubectl request"));
    const logState = !logs.startup_complete ? "warn" : (logs.dns_timeouts || logs.recent_errors) ? "bad" : "ok";
    const logParts = [logs.startup_complete ? "startup complete" : "still starting"];
    if (logs.proxy_listening) logParts.push("API proxy listening on :443");
    if (logs.dns_timeouts) logParts.push(`${logs.dns_timeouts} DNS/API timeouts`);
    if (logs.recent_errors) logParts.push(`${logs.recent_errors} recent errors, last: ${logs.last_error}`);
    items.push(check(logState, "Operator logs", logParts.join(" · ")));
    const nodes = (network.nodes || []).map((row) => `${row.node} ${row.address || "no address"}${row.tailscale ? "" : " (not Tailscale)"}`);
    items.push(check(network.ok ? "ok" : "bad", "Pod network", nodes.length ? `flannel over ${nodes.join(", ")}` : "no nodes reported"));
    element("checks").replaceChildren(...items);
  }

  function render(status) {
    installed = !!status.installed;
    const rows = [];
    if (status.error) {
      rows.push(row("Status", `unavailable: ${status.error}`));
    } else if (!status.installed) {
      rows.push(row("Status", "operator not installed"));
    } else {
      const ready = `${status.ready_replicas}/${status.replicas || 1} ready`;
      rows.push(row("Status", `${status.helm_status} · ${ready}`));
      rows.push(row("Version", `chart ${status.chart_version || "?"} · operator ${status.app_version || "?"}`));
      rows.push(row("API proxy", status.proxy_mode === "off" ? "off" : `${status.proxy_mode} mode`));
      if (status.proxy_url) rows.push(row("Proxy URL", code(status.proxy_url)));
      if (status.kubeconfig_command) rows.push(row("Connect", code(status.kubeconfig_command)));
      element("hostname").value = status.hostname || element("hostname").value;
      if (status.proxy_mode === "auth" || status.proxy_mode === "noauth") element("mode").value = status.proxy_mode;
    }
    rows.push(row("Read-only binding", status.readers_binding ? "tailnet-readers → view" : "not created"));
    statusList.replaceChildren(...rows.flat());
    const reason = status.error ? "Cluster unavailable, so there is nothing to check." : "Operator not installed. Install it below.";
    renderLive(status.installed && !status.error ? status.live : null, status.hostname, reason);
    element("setup-title").textContent = installed ? "Reconfigure / upgrade operator" : "Install operator";
    element("readers").checked = element("readers").checked || !!status.readers_binding;
    element("install").textContent = installed ? "Upgrade operator" : "Install operator";
    buttons();
  }

  function buttons() {
    element("install").disabled = busy;
    element("refresh").disabled = busy;
  }

  async function request(method, payload) {
    const response = await fetch("/api/kubernetes/tailscale-operator", {
      method, headers: { "Content-Type": "application/json" },
      ...(payload ? { body: JSON.stringify(payload) } : {}),
    });
    const data = await response.json().catch(() => ({ ok: false, error: `Server returned ${response.status}.` }));
    if (!response.ok || !data.ok) throw new Error(data.error || `Request failed (${response.status}).`);
    return data;
  }

  async function refresh() {
    try {
      render((await request("GET")).status);
    } catch (error) {
      choosePanel(false);
      element("message").textContent = error.message;
    }
  }

  async function install() {
    const action = installed ? "Upgrade" : "Install";
    if (!window.confirm(`${action} the Tailscale operator in namespace "tailscale" on the configured cluster?`)) return;
    busy = true;
    buttons();
    element("message").textContent = `${action} in progress; this waits for the operator to become ready (up to a few minutes)...`;
    const secretInput = element("client-secret");
    try {
      const data = await request("POST", {
        confirm: true,
        hostname: element("hostname").value.trim(),
        proxy_mode: element("mode").value,
        client_id: element("client-id").value.trim(),
        client_secret: secretInput.value.trim(),
        readers_binding: element("readers").checked,
      });
      element("message").textContent = `${action} complete (Helm revision ${data.revision ?? "?"}). The first kubectl call may be slow while the proxy fetches its TLS certificate.`;
      render(data.status);
    } catch (error) {
      element("message").textContent = error.message;
    } finally {
      secretInput.value = "";
      busy = false;
      buttons();
    }
  }

  element("install").addEventListener("click", install);
  element("refresh").addEventListener("click", refresh);
  refresh();
  setInterval(() => {
    if (!busy && !document.hidden) refresh();
  }, 30000);
})();
