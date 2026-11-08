(() => {
  const form = document.getElementById("helm-form");
  const element = (name) => document.getElementById(`helm-${name}`);
  let records = [];
  let examples = [];
  let current = null;
  let dirty = false;
  let busy = false;

  function buttons() {
    element("save").disabled = busy || !current;
    element("preview").disabled = busy || !current || dirty;
    element("deploy").disabled = busy || !current || dirty;
    element("import").disabled = busy || !!current;
  }

  function show(record) {
    current = record;
    dirty = false;
    element("editor").hidden = !record;
    element("repository").value = record ? record.repository : "";
    element("chart-path").value = record ? record.chart_path : "";
    element("repository").readOnly = !!record;
    element("chart-path").readOnly = !!record;
    element("manifest").textContent = "Preview before deploying.";
    if (record) {
      for (const name of ["name", "release", "namespace", "values"]) element(name).value = record[name];
      element("source").textContent = `${record.chart_name} ${record.chart_version} · ${record.chart_path} · pinned commit ${record.commit}`;
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
    const select = element("selection");
    select.replaceChildren(new Option("New configuration", ""));
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
    show(records.find((record) => record.id === element("selection").value) || null);
    if (element("selection").value.startsWith("example:")) {
      const sample = examples[Number(element("selection").value.split(":")[1])];
      element("repository").value = sample.repository;
      element("chart-path").value = sample.chart_path;
    }
    element("status").textContent = "";
  });
  element("new").addEventListener("click", () => {
    element("selection").value = "";
    show(null);
    element("status").textContent = "";
  });
  for (const name of ["name", "release", "namespace", "values"]) {
    element(name).addEventListener("input", () => {
      dirty = true;
      element("status").textContent = "Unsaved changes. Save before previewing or deploying.";
      buttons();
    });
  }
  form.addEventListener("submit", (event) => event.preventDefault());
  element("import").addEventListener("click", () => operation("Importing chart and default values...", async () => {
    const data = await request("/import", "POST", {
      repository: element("repository").value, chart_path: element("chart-path").value,
    });
    await refresh(data.configuration.id);
    element("status").textContent = "Defaults imported and configuration saved. No cluster changes made.";
  }));
  element("save").addEventListener("click", () => operation("Saving configuration...", async () => {
    const payload = { revision: current.revision };
    for (const name of ["name", "release", "namespace", "values"]) payload[name] = element(name).value;
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
  element("deploy").addEventListener("click", () => {
    if (!window.confirm(`Deploy / upgrade ${current.release} in ${current.namespace} on the configured cluster? This may create or change resources and run chart hooks.`)) return;
    operation("Submitting saved configuration (may take up to two minutes)...", async () => {
      const id = current.id;
      try {
        await request(`/${id}/deploy`, "POST", { revision: current.revision, confirm: true });
      } finally {
        await refresh(id);
      }
      element("status").textContent = "Helm release deployed. Configuration and submitted values retained.";
      loadKubernetesSummary();
    });
  });
  operation("Loading Helm configurations...", async () => {
    await refresh();
    element("status").textContent = "Select an example or enter a GitHub repository to start.";
  });
})();
