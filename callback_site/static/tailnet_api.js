(() => {
  const element = (name) => document.getElementById(`tailnet-api-${name}`);
  const statusList = element("status");
  if (!statusList) return;
  let busy = false;
  let installed = false;

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
})();
