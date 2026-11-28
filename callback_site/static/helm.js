(() => {
  const form = document.getElementById("helm-form");
  const element = (name) => document.getElementById(`helm-${name}`);
  let records = [];
  let examples = [];
  let bundled = [];
  let current = null;
  let dirty = false;
  let busy = false;
  const tailscaleFields = ["mode", "service", "hostname", "port", "proxy-class", "proxy-group", "tags"];
  const servarrFields = ["secret-name", "hostname", "tag", "exit-node", "pod-cidr", "cluster-dns", "puid"];

  function buttons() {
    element("save").disabled = busy || !current;
    element("preview").disabled = busy || !current || dirty;
    element("refresh-source").disabled = busy || !current || dirty;
    element("release-status").disabled = busy || !current || dirty;
    element("servarr-diff").disabled = busy || !current || dirty;
    element("deploy").disabled = busy || !current || dirty;
    element("dockerbot-token-save").disabled = busy || !current || dirty;
    element("import").disabled = busy || !!current;
  }

  function show(record) {
    current = record;
    dirty = false;
    element("dockerbot-token").value = "";
    element("editor").hidden = !record;
    element("repository").value = record ? record.repository : "";
    element("chart-path").value = record ? record.chart_path : "";
    element("repository").readOnly = !!record;
    element("chart-path").readOnly = !!record;
    element("repository-fields").hidden = !!record?.bundled || element("selection").value.startsWith("bundled:");
    element("manifest").textContent = "Preview before deploying.";
    element("release-status-text").textContent = "";
    if (record) {
      for (const name of ["name", "release", "namespace", "values"]) element(name).value = record[name];
      const tailscale = record.tailscale || { mode: "off" };
      for (const name of tailscaleFields) element(`ts-${name}`).value = tailscale[name.replaceAll("-", "_")] || "";
      element("source").textContent = record.bundled
        ? `${record.chart_name} ${record.chart_version} · bundled snapshot ${record.commit}`
        : `${record.chart_name} ${record.chart_version} · ${record.chart_path} · pinned commit ${record.commit}`;
      element("guide").value = record.guide || "No setup guide was included with this chart. Consult the chart's upstream documentation.";
      element("dockerbot-token-panel").hidden = !(record.bundled === "dockerbot" ||
        ((record.repository || "").toLowerCase() === "https://github.com/mevsthevoices/playbook" && record.chart_path === "charts/dockerbot"));
      element("servarr-panel").hidden = record.bundled !== "servarr";
      element("servarr-diff").hidden = record.bundled !== "servarr" || record.deploy_method !== "kubectl";
      element("release-status").hidden = record.deploy_method === "kubectl";
      if (record.bundled === "servarr") {
        element("servarr-method").value = record.deploy_method;
        element("servarr-enabled").value = record.servarr_vpn.enabled ? "on" : "off";
        for (const name of servarrFields) element(`servarr-${name}`).value = record.servarr_vpn[name.replaceAll("-", "_")];
      }
      const submission = record.last_submission;
      element("submission").textContent = submission
        ? `Last submission: ${submission.status} · ${submission.context} (${submission.server}) · ${submission.namespace}/${submission.release} · saved revision ${submission.revision}${submission.error ? ` · ${submission.error}` : ""}`
        : "Not submitted to a cluster yet.";
    }
    buttons();
  }

  async function request(path, method = "GET", payload) {
    const response = await fetch(`/api/helm/configurations${path}`, {
      method, headers: { "Content-Type": "application/json" },
      ...(payload ? { body: JSON.stringify(payload) } : {}),
    });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || `Helm request failed (${response.status}).`);
    return data;
  }

  async function refresh(selected = "") {
    const data = await request("");
    records = data.configurations;
    examples = data.examples;
    bundled = data.bundled || [];
    const select = element("selection");
    select.replaceChildren(new Option("New configuration", ""));
    const bundledGroup = document.createElement("optgroup");
    bundledGroup.label = "Bundled charts";
    bundled.forEach((chart) => bundledGroup.append(new Option(chart.name, `bundled:${chart.id}`)));
    select.append(bundledGroup);
    const sampleGroup = document.createElement("optgroup");
    sampleGroup.label = "Included examples";
    examples.forEach((sample, index) => sampleGroup.append(new Option(sample.name, `example:${index}`)));
    select.append(sampleGroup);
    const savedGroup = document.createElement("optgroup");
    savedGroup.label = "Saved configurations";
    records.forEach((record) => savedGroup.append(new Option(`${record.name} (${record.namespace}/${record.release})`, record.id)));
    select.append(savedGroup);
    select.value = selected;
    show(records.find((record) => record.id === selected) || null);
  }

  async function operation(message, task) {
    busy = true;
    for (const control of form.elements) control.disabled = true;
    element("status").textContent = message;
    try {
      await task();
    } catch (error) {
      element("status").textContent = error.message;
    } finally {
      busy = false;
      for (const control of form.elements) control.disabled = false;
      buttons();
    }
  }

  element("selection").addEventListener("change", () => {
    if (element("selection").value) element("panel").open = true;
    show(records.find((record) => record.id === element("selection").value) || null);
    if (element("selection").value.startsWith("example:")) {
      const sample = examples[Number(element("selection").value.split(":")[1])];
      element("repository").value = sample.repository;
      element("chart-path").value = sample.chart_path;
    }
    element("status").textContent = element("selection").value.startsWith("bundled:")
      ? "Select Import defaults to create a configuration from this bundled chart." : "";
  });
  element("new").addEventListener("click", () => {
    element("panel").open = true;
    element("selection").value = "";
    show(null);
    element("status").textContent = "";
  });
  for (const name of ["name", "release", "namespace", "values", ...tailscaleFields.map((field) => `ts-${field}`),
    "servarr-method", "servarr-enabled", ...servarrFields.map((field) => `servarr-${field}`)]) {
    element(name).addEventListener("input", () => {
      dirty = true;
      element("status").textContent = "Unsaved changes. Save before previewing or deploying.";
      buttons();
    });
  }
  form.addEventListener("submit", (event) => event.preventDefault());
  element("servarr-kubby").addEventListener("click", () => {
    if (!window.confirm("Replace the editor values and target with the reconstructed Kubby preset? Unsaved edits will be lost.")) return;
    element("release").value = "servarr";
    element("namespace").value = "servarr";
    element("values").value = current.kubby_values;
    element("servarr-method").value = "kubectl";
    element("servarr-enabled").value = current.kubby_vpn.enabled ? "on" : "off";
    for (const name of servarrFields) element(`servarr-${name}`).value = current.kubby_vpn[name.replaceAll("-", "_")];
    element("ts-mode").value = "off";
    for (const name of tailscaleFields.slice(1)) element(`ts-${name}`).value = "";
    dirty = true;
    element("status").textContent = "Kubby preset loaded. Save and review the preview before applying.";
    buttons();
  });
  element("import").addEventListener("click", () => operation("Importing chart and default values...", async () => {
    const selection = element("selection").value;
    const payload = selection.startsWith("bundled:")
      ? { bundled: selection.slice("bundled:".length) }
      : { repository: element("repository").value, chart_path: element("chart-path").value };
    const data = await request("/import", "POST", payload);
    await refresh(data.configuration.id);
    element("status").textContent = "Defaults imported and configuration saved. No cluster changes made.";
  }));
  element("save").addEventListener("click", () => operation("Saving configuration...", async () => {
    const payload = { revision: current.revision };
    for (const name of ["name", "release", "namespace", "values"]) payload[name] = element(name).value;
    payload.tailscale = Object.fromEntries(tailscaleFields.map((name) => [name.replaceAll("-", "_"), element(`ts-${name}`).value]));
    if (current.bundled === "servarr") {
      payload.deploy_method = element("servarr-method").value;
      payload.servarr_vpn = { enabled: element("servarr-enabled").value === "on",
        ...Object.fromEntries(servarrFields.map((name) => [name.replaceAll("-", "_"), element(`servarr-${name}`).value])) };
    }
    const data = await request(`/${current.id}`, "PUT", payload);
    await refresh(data.configuration.id);
    element("status").textContent = "Configuration saved. No cluster changes made.";
  }));
  element("preview").addEventListener("click", () => operation("Rendering the saved configuration...", async () => {
    const data = await request(`/${current.id}/preview`, "POST", { revision: current.revision });
    element("manifest").textContent = data.manifest;
    element("manifest").closest("details").open = true;
    element("status").textContent = "Manifests rendered. No cluster changes made.";
  }));
  element("refresh-source").addEventListener("click", () => {
    if (!window.confirm("Refresh this configuration from the latest chart source? Saved values and release are kept. Preview and review changes before deploying; this does not change the cluster.")) return;
    operation("Refreshing chart source and pinned dependencies...", async () => {
      const data = await request(`/${current.id}/refresh-source`, "POST", { revision: current.revision, confirm: true });
      await refresh(data.configuration.id);
      element("status").textContent = "Chart source refreshed. Values were preserved; preview and review before deploying.";
    });
  });
  element("release-status").addEventListener("click", () => operation("Checking the Helm release...", async () => {
    const data = await request(`/${current.id}/status`, "POST", { revision: current.revision });
    const status = data.status;
    element("release-status-text").textContent = `Live release: ${status.status} · ${status.namespace}/${status.release} · Helm revision ${status.helm_revision}${status.description ? ` · ${status.description}` : ""}`;
    element("status").textContent = "Live Helm release status checked. No cluster changes made.";
  }));
  element("servarr-diff").addEventListener("click", () => operation("Comparing rendered resources with the cluster...", async () => {
    const data = await request(`/${current.id}/diff`, "POST", { revision: current.revision });
    element("manifest").textContent = data.diff;
    element("manifest").closest("details").open = true;
    element("status").textContent = data.changed ? "Differences found. Review before applying." : "No differences. No cluster changes made.";
  }));
  element("dockerbot-token-save").addEventListener("click", () => {
    if (!window.confirm(`Create or update the Dockerbot Secret in ${current.namespace} on the configured cluster?`)) return;
    operation("Writing the Secret to Kubernetes...", async () => {
      try {
        await request(`/${current.id}/dockerbot-token`, "POST", {
          revision: current.revision, confirm: true, token: element("dockerbot-token").value,
        });
        element("status").textContent = "Secret updated. Restart an existing bot pod to use the new token.";
      } finally {
        element("dockerbot-token").value = "";
      }
    });
  });
  element("deploy").addEventListener("click", () => {
    const kubectl = current.bundled === "servarr" && current.deploy_method === "kubectl";
    const action = kubectl ? "Apply rendered manifests without pruning (no Helm release)" : "Deploy / upgrade the Helm release";
    if (!window.confirm(`${action}: ${current.release} in ${current.namespace} on the configured cluster? This may create or change resources${kubectl ? "." : " and run chart hooks."}`)) return;
    let direct = false;
    if (current.bundled === "servarr" && !current.servarr_vpn.enabled) {
      if (!window.confirm("The qBittorrent VPN is OFF. Deploying may remove an existing VPN guard and allow direct network egress. Continue?")) return;
      direct = true;
    }
    operation(`Submitting saved configuration (${current.servarr_vpn?.enabled && !kubectl ? "may take up to six minutes" : "may take up to two minutes"})...`, async () => {
      const id = current.id;
      try {
        await request(`/${id}/deploy`, "POST", { revision: current.revision, confirm: true, confirm_direct_egress: direct });
      } finally {
        await refresh(id);
      }
      element("status").textContent = kubectl ? "Manifests applied without pruning. Check workload rollouts and VPN egress." : "Helm release deployed. Configuration and submitted values retained.";
      loadKubernetesSummary();
    });
  });
  operation("Loading Helm configurations...", async () => {
    await refresh();
    element("status").textContent = "Select an example or enter a GitHub repository to start.";
  });
  fetch("/api/kubernetes/tailscale-operator")
    .then(async (response) => {
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || "Operator check failed.");
      const status = data.status;
      if (status.error) throw new Error(status.error);
      element("tailscale-status").textContent = status.installed && status.ready_replicas
        ? "Tailscale operator is ready. Exposure is applied only on deployment."
        : "Tailscale operator is not ready. Install it before deploying exposure.";
    })
    .catch((error) => { element("tailscale-status").textContent = `Operator check failed: ${error.message}`; });
})();
