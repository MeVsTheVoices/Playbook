const K3S_ROLE_HINTS = {
  pi3bplus: "A Pi 3B+ has about 900 MB of RAM, below the 2 GB K3s documents for a server. light-worker joins as a K3s agent (no etcd, no API server). For a utility node that runs no Kubernetes, set Distribution to Standard instead.",
  pi5: "worker joins as a K3s agent. control-plane makes this Pi a K3s server and an etcd voter: keep the number of servers odd (the NucBox plus two Pi 5 makes three). For a utility node that runs no Kubernetes, set Distribution to Standard instead.",
  pi4: "worker joins as a K3s agent. control-plane makes this Pi a K3s server and an etcd voter: K3s says SD cards cannot handle etcd IO, so boot it from a USB SSD, and keep the number of servers odd. On a 1 to 2 GB Pi 4 consider turning on the lightweight server options. For a utility node that runs no Kubernetes, set Distribution to Standard instead.",
};

function k3sRoleLabel(profile, role) {
  const d = K3S_ROLE_DEFAULTS[profile] || {};
  if (role === d.role) return `${role} (recommended)`;
  if (profile === "pi4" && role === "control-plane") return `${role} (needs an SSD)`;
  return profile === "pi3bplus" && role === "control-plane" ? `${role} (not recommended)` : role;
}

function updateK3sRoleHints() {
  const profile = String(form.profile?.value || "").trim().toLowerCase();
  const role = String(document.getElementById("opt_k3s_role")?.value || "");
  const roleHint = document.getElementById("k3s-role-hint");
  if (roleHint) roleHint.textContent = K3S_ROLE_HINTS[profile] || "";
  const taintHint = document.getElementById("k3s-taint-hint");
  if (taintHint) {
    const taint = K3S_ROLE_TAINTS[role] || "";
    taintHint.textContent = taint ? `Adds ${taint}; only pods that tolerate it are scheduled here.` : "";
  }
}

// Rebuilds the role list and resets every board-dependent default; call on profile change, not when loading a template.
function applyK3sRoleDefaults() {
  const profile = String(form.profile?.value || "").trim().toLowerCase();
  const d = K3S_ROLE_DEFAULTS[profile];
  const select = document.getElementById("opt_k3s_role");
  if (!d || !select) return;
  select.innerHTML = "";
  for (const role of d.roles) {
    const option = document.createElement("option");
    option.value = role;
    option.textContent = k3sRoleLabel(profile, role);
    select.appendChild(option);
  }
  // Always back to the board's safe default: a server role must never carry over by accident.
  select.value = d.role;
  select.dataset.optionDefault = d.role;
  const taint = document.getElementById("opt_k3s_taint");
  taint.checked = Boolean(d.taint);
  taint.dataset.optionDefault = String(Boolean(d.taint));
  const maxPods = document.getElementById("opt_k3s_max_pods");
  maxPods.value = d.max_pods;
  maxPods.dataset.optionDefault = d.max_pods;
  document.getElementById("opt_k3s_ingress").value = d.ingress;
  for (const [optionId, on] of Object.entries(d.server)) {
    const box = document.getElementById(`opt_${optionId}`);
    if (!box) continue;
    box.checked = Boolean(on);
    box.dataset.optionDefault = String(Boolean(on));
  }
  updateK3sRoleHints();
}

function updateOsScopedOptionsVisibility() {
  for (const el of sdOptionsInputEls) {
    const wrapper = el.closest("[data-os-option-wrapper='1']");
    if (!wrapper) continue;
    const visible = optionScopeMatches(el);
    wrapper.style.display = visible ? "" : "none";
    el.disabled = !visible;
    // Profile-scoped options keep their value while hidden; gather skips them, so
    // nothing is sent for the wrong board and the choice returns when switching back.
    if (!visible && !el.dataset.optionProfile && String(el.dataset.optionType || "") === "checkbox") {
      el.checked = false;
    }
  }
  updateSdOptionsConfiguredCount();
}
