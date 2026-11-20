(() => {
  const rows = document.getElementById("rbac-rows");
  if (!rows) return;
  const nameInput = document.getElementById("rbac-name");
  const roleInput = document.getElementById("rbac-role");
  const subjectsInput = document.getElementById("rbac-subjects");
  const message = document.getElementById("rbac-message");
  const saveButton = document.getElementById("rbac-save");
  let bindings = [];

  async function request(method, path, payload) {
    const response = await fetch(path, {
      method, headers: { "Content-Type": "application/json" },
      ...(payload ? { body: JSON.stringify(payload) } : {}),
    });
    const data = await response.json().catch(() => ({ ok: false, error: `Server returned ${response.status}.` }));
    if (!response.ok || !data.ok) throw new Error(data.error || `Request failed (${response.status}).`);
    return data;
  }

  function cell(text) {
    const td = document.createElement("td");
    td.textContent = text;
    return td;
  }

  function button(label, action, name) {
    const element = document.createElement("button");
    element.type = "button";
    element.className = "kube-btn";
    element.textContent = label;
    element.dataset.rbacAction = action;
    element.dataset.rbacName = name;
    return element;
  }

  function render() {
    if (!bindings.length) {
      const tr = document.createElement("tr");
      const td = cell("No Playbook-managed bindings yet.");
      td.colSpan = 4;
      tr.append(td);
      rows.replaceChildren(tr);
      return;
    }
    rows.replaceChildren(...bindings.map((binding) => {
      const tr = document.createElement("tr");
      const actions = document.createElement("td");
      actions.append(button("Edit", "edit", binding.name), " ", button("Delete", "delete", binding.name));
      tr.append(
        cell(binding.name),
        cell(binding.role),
        cell(binding.subjects.map((s) => `${s.kind} ${s.name}`).join(", ")),
        actions,
      );
      return tr;
    }));
  }

  async function refresh() {
    try {
      bindings = (await request("GET", "/api/kubernetes/rbac/bindings")).bindings || [];
      render();
    } catch (error) {
      rows.replaceChildren();
      message.textContent = error.message;
    }
  }

  function parseSubjects() {
    return subjectsInput.value.split("\n").map((line) => line.trim()).filter(Boolean).map((line) => {
      const [first, ...rest] = line.split(/\s+/);
      if (!rest.length) return { kind: "Group", name: first };
      const kind = first.charAt(0).toUpperCase() + first.slice(1).toLowerCase();
      return { kind, name: rest.join(" ") };
    });
  }

  async function save() {
    const name = nameInput.value.trim();
    const role = roleInput.value;
    const subjects = parseSubjects();
    if (!name || !subjects.length) {
      message.textContent = "Enter a binding name and at least one subject.";
      return;
    }
    const lines = subjects.map((s) => `- ${s.kind} ${s.name}`).join("\n");
    const warning = role === "cluster-admin" ? "\n\ncluster-admin grants FULL control of the cluster." : "";
    if (!window.confirm(`Bind ClusterRole "${role}" as ${name} to:\n\n${lines}${warning}`)) return;
    saveButton.disabled = true;
    try {
      const data = await request("PUT", `/api/kubernetes/rbac/bindings/${encodeURIComponent(name)}`, { role, subjects, confirm: true });
      message.textContent = `${data.created ? "Created" : "Updated"} ${name}.`;
      await refresh();
    } catch (error) {
      message.textContent = error.message;
    } finally {
      saveButton.disabled = false;
    }
  }

  async function remove(name) {
    if (!window.confirm(`Delete ClusterRoleBinding ${name}? Its subjects lose this access immediately.`)) return;
    try {
      await request("DELETE", `/api/kubernetes/rbac/bindings/${encodeURIComponent(name)}`, { confirm: true });
      message.textContent = `Deleted ${name}.`;
      await refresh();
    } catch (error) {
      message.textContent = error.message;
    }
  }

  function edit(name) {
    const binding = bindings.find((b) => b.name === name);
    if (!binding) return;
    nameInput.value = binding.name;
    roleInput.value = binding.role;
    subjectsInput.value = binding.subjects.map((s) => `${s.kind} ${s.name}`).join("\n");
    message.textContent = `Editing ${name}. Changing the role recreates the binding.`;
  }

  rows.addEventListener("click", (event) => {
    const target = event.target.closest("[data-rbac-action]");
    if (!target) return;
    if (target.dataset.rbacAction === "edit") edit(target.dataset.rbacName);
    else remove(target.dataset.rbacName);
  });
  saveButton.addEventListener("click", save);
  document.getElementById("rbac-refresh").addEventListener("click", refresh);
  document.getElementById("rbac-clear").addEventListener("click", () => {
    document.getElementById("rbac-form").reset();
    message.textContent = "";
  });
  refresh();
})();
