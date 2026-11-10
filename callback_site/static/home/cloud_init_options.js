const GENERAL_CLOUD_INIT_SECTIONS = [
  "k3s", "identity", "network", "packages", "misc", "hardening", "access", "tailscale", "telemetry",
];
const SD_SAVED_SECRET = "__CONFIGURED__";
const SD_SECRET_KEYS = new Set(["tailscale_auth_key", "toolkit_wifi_password", "toolkit_terminal_password_hash", "password", "telegraf_influx_token"]);
let sdBrowserSessionReady = Promise.resolve();

async function saveSdBrowserSelection(selection) {
  await sdBrowserSessionReady;
  const res = await fetch("/api/sd/options-profiles/session", {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(selection),
  });
  const data = await res.json();
  if (!res.ok || !data.ok) throw new Error(data.error || "Could not retain this browser-session selection.");
}

function updateSdSavedSecretHint(el) {
  const hint = document.getElementById(`${el.id}-saved-hint`);
  if (hint) hint.textContent = el.value === SD_SAVED_SECRET
    ? "Saved for this browser session. Replace to update; clear and save to remove."
    : "";
}
const CLOUD_INIT_PROFILE_MATRIX = {
  "uefi-x86_64": { sections: ["identity", "access"], optionKeys: ["username", "timezone", "locale"] },
  "uefi-arm64": { sections: ["identity", "access"], optionKeys: ["username", "timezone", "locale"] },
  pi5: { sections: [...GENERAL_CLOUD_INIT_SECTIONS, "pi"] },
  pi4: { sections: GENERAL_CLOUD_INIT_SECTIONS },
  pi3bplus: { sections: GENERAL_CLOUD_INIT_SECTIONS },
  "network-toolkit": {
    sections: ["toolkit", "toolkit-tools", "identity", "access", "tailscale", "telemetry"],
    optionKeys: [
      "toolkit_terminal_password_hash", "toolkit_enable_funnel",
      "toolkit_wifi_ssid", "toolkit_wifi_password", "toolkit_wifi_country",
      "toolkit_extra_packages",
      "full_name", "timezone", "locale", "username", "shell", "extra_ssh_keys",
      "tailscale_ssh", "tailscale_auth_key", "tailscale_hostname",
      "enable_power_restore_callback", "boot_summary", "enable_unattended_upgrades",
    ],
  },
  "playbook-server": {
    sections: ["server", "identity", "network", "packages", "misc", "hardening", "access", "tailscale", "telemetry"],
  },
};
const SD_FIXED_STANDARD_PROFILES = new Set(["network-toolkit", "playbook-server"]);

function cloudInitProfileScopeMatches(el) {
  const matrix = CLOUD_INIT_PROFILE_MATRIX[form.profile.value];
  const section = el.closest("[data-cloud-init-section]");
  if (!matrix || !section || !matrix.sections.includes(section.dataset.cloudInitSection)) return false;
  if (section.dataset.cloudInitSection === "k3s" && form.distribution.value !== "k3s") return false;
  return !matrix.optionKeys || matrix.optionKeys.includes(el.dataset.optionKey);
}

function updateCloudInitSectionsVisibility() {
  const matrix = CLOUD_INIT_PROFILE_MATRIX[form.profile.value];
  const toolkit = form.profile.value === "network-toolkit";
  for (const section of document.querySelectorAll("[data-cloud-init-section]")) {
    const key = section.dataset.cloudInitSection;
    section.hidden = !matrix?.sections.includes(key) || (key === "k3s" && form.distribution.value !== "k3s");
  }
  sdOptionsSecurityDefaultsBtn.hidden = toolkit || isUefiInstaller();
  const authKey = document.getElementById("opt_tailscale_auth_key");
  authKey.required = toolkit;
  document.querySelector("label[for='opt_tailscale_auth_key']").textContent = toolkit
    ? "Tailscale Auth Key (required, authorized for tag:network-toolkit)"
    : "Tailscale Auth Key (optional)";
  document.getElementById("tailscale-bootstrap-hint").textContent = toolkit
    ? "Tailscale is always installed with tag:network-toolkit. Private SSH uses your public key; enable Tailscale SSH only with an appropriate tailnet SSH policy. Routing and exit nodes are disabled."
    : "Optional fields for first-boot Tailscale join and tags.";
  document.getElementById("telemetry-section-title").textContent = toolkit
    ? "Boot Reporting and Updates" : "Hardware Detection and Telemetry";
  document.getElementById("telemetry-section-hint").textContent = toolkit
    ? "Hardware detection is automatic. Keep boot callbacks and optional security updates without installing a telemetry agent."
    : "Enable basic hardware checks and reporting, or optional hardening controls by OS family.";
}

function ensureK3sConfigDefaults() {
  const toolkit = form.profile.value === "network-toolkit";
  const fixedStandard = SD_FIXED_STANDARD_PROFILES.has(form.profile.value) || isUefiInstaller();
  if (fixedStandard) form.distribution.value = "standard";
  form.distribution.disabled = fixedStandard;
  updateCloudInitSectionsVisibility();
  const toolkitSection = document.getElementById("network-toolkit-section");
  if (toolkitSection) {
    toolkitSection.hidden = !toolkit;
    toolkitSection.open = toolkit;
  }
  if (toolkit) {
    document.getElementById("cloud-init-optional-settings").open = true;
    document.querySelector("[data-cloud-init-section='tailscale']").open = true;
  }
  const textarea = document.getElementById("opt_k3s_config");
  const section = document.getElementById("k3s-distribution-section");
  const isK3sSelected = String(form.distribution.value || "standard").trim().toLowerCase() === "k3s";
  if (section) {
    section.open = isK3sSelected;
  }
  if (!textarea) return;
  const value = String(textarea.value || "").trim();
  if (isK3sSelected && !value) {
    textarea.value = DEFAULT_K3S_CONFIG.trimEnd();
  }
}

function gatherCloudInitOptions() {
  const options = {};
  for (const el of sdOptionsInputEls) {
    if (el.disabled) {
      continue;
    }
    if (!optionScopeMatches(el)) {
      continue;
    }
    const key = String(el.dataset.optionKey || "").trim();
    if (!key) continue;
    const kind = String(el.dataset.optionType || "").trim();
    if (kind === "checkbox") {
      if (el.checked) {
        options[key] = true;
      } else if (String(el.dataset.optionDefault || "").trim().toLowerCase() === "true") {
        options[key] = false;
      }
      continue;
    }

    const inputValue = String(el.value || "");
    const raw = key === "toolkit_wifi_password" || key === "toolkit_wifi_ssid" ? inputValue : inputValue.trim();
    if (!raw) continue;
    if (kind === "csv") {
      const values = raw.split(",").map((v) => v.trim()).filter(Boolean);
      if (values.length) options[key] = values;
      continue;
    }
    if (kind === "lines") {
      const values = raw.split("\n").map((v) => v.trim()).filter(Boolean);
      if (values.length) options[key] = values;
      continue;
    }
    options[key] = raw;
  }
  if (String(form.distribution.value || "standard").trim().toLowerCase() === "k3s") {
    const k3sValue = String(document.getElementById("opt_k3s_config")?.value || "").trim();
    if (!k3sValue) {
      options.k3s_config = DEFAULT_K3S_CONFIG.trimEnd();
    } else {
      options.k3s_config = k3sValue;
    }
  }
  if (form.profile.value === "network-toolkit") {
    options.toolkit_enable_funnel = document.getElementById("opt_toolkit_enable_funnel").checked;
  }
  return options;
}

function updateSdOptionsConfiguredCount() {
  const count = Object.keys(gatherCloudInitOptions()).length;
  sdOptionsConfigCountEl.textContent = `${count} field${count === 1 ? "" : "s"} set`;
}

function updateSdOptionActions() {
  const hasSelected = Boolean(String(sdOptionsProfileSelectEl.value || "").trim());
  sdOptionsDeleteBtn.disabled = !hasSelected;
  sdOptionsSaveBtn.disabled = false;
}

function updatePiScopedOptionsVisibility() {
  const section = document.getElementById("pi-specific-section");
  const piOnlySelected = Boolean(sdOptionsProfilePiOnlyEl && sdOptionsProfilePiOnlyEl.checked);
  const selectedProfile = String(sdOptionsProfileSelectEl.value || "").trim();
  const selectedProfilePiOnly = Boolean(selectedProfile && sdOptionProfiles[selectedProfile] && sdOptionProfiles[selectedProfile].pi_only);
  const visible = (piOnlySelected || selectedProfilePiOnly) &&
    Boolean(CLOUD_INIT_PROFILE_MATRIX[form.profile.value]?.sections.includes("pi"));
  const piModel = String(form.profile.value || "").trim().toLowerCase();
  const piModelSelectEl = document.getElementById("pi_model_select");
  if (piModelSelectEl && piModelSelectEl.value !== piModel) {
    piModelSelectEl.value = piModel;
  }

  if (section) {
    section.style.display = visible ? "" : "none";
    section.open = visible;
  }

  let shownModelOptions = 0;
  for (const el of sdOptionsInputEls) {
    const wrapper = el.closest("[data-pi-option-wrapper='1']");
    if (!wrapper) continue;
    const wrapperModel = String(wrapper.dataset.piModel || "").trim().toLowerCase();
    const wrapperVisible = visible && (!wrapperModel || wrapperModel === piModel) && optionScopeMatches(el);
    wrapper.style.display = wrapperVisible ? "" : "none";
    el.disabled = !wrapperVisible;
    if (wrapperVisible) shownModelOptions += 1;
  }

  const noOptionsEl = document.getElementById("pi-model-no-options");
  if (noOptionsEl) {
    noOptionsEl.style.display = visible && shownModelOptions === 0 ? "" : "none";
  }
}

function applyCloudInitOptions(options) {
  const data = options && typeof options === "object" ? options : {};
  for (const el of sdOptionsInputEls) {
    const key = String(el.dataset.optionKey || "").trim();
    const kind = String(el.dataset.optionType || "").trim();
    const value = data[key];
    if (SD_SECRET_KEYS.has(key)) {
      el.dataset.savedSecret = value === SD_SAVED_SECRET ? "1" : "";
    }
    if (kind === "checkbox") {
      const defaultChecked = String(el.dataset.optionDefault || "").trim().toLowerCase() === "true";
      el.checked = value === undefined || value === null ? defaultChecked : Boolean(value);
      continue;
    }
    if (kind === "csv") {
      el.value = Array.isArray(value) ? value.join(", ") : "";
      continue;
    }
    if (kind === "lines") {
      el.value = Array.isArray(value) ? value.join("\n") : "";
      continue;
    }
    el.value = value === undefined || value === null ? String(el.dataset.optionDefault || "") : String(value);
    if (SD_SECRET_KEYS.has(key)) updateSdSavedSecretHint(el);
  }
  updateOsScopedOptionsVisibility();
  updatePiScopedOptionsVisibility();
  updateSdOptionsConfiguredCount();
}

async function resetCloudInitOptions() {
  await sdBrowserSessionReady;
  applyCloudInitOptions({});
  sdOptionsProfileSelectEl.value = "";
  if (sdOptionsProfilePiOnlyEl) {
    sdOptionsProfilePiOnlyEl.checked = false;
  }
  updatePiScopedOptionsVisibility();
  updateSdOptionActions();
  sdOptionsProfileStatusEl.textContent = "Optional cloud-init fields reset. Fill manually or load a template.";
  try {
    await saveSdBrowserSelection({name: ""});
  } catch (error) {
    sdOptionsProfileStatusEl.textContent = `Fields reset, but session selection could not be cleared: ${error.message}`;
  }
}

function applySecurityStarterDefaults() {
  const defaults = {
    disable_ssh_password_auth: true,
    disable_root_ssh: true,
    boot_summary: true,
    enable_fail2ban: true,
    enable_unattended_upgrades: true,
  };
  applyCloudInitOptions({
    ...gatherCloudInitOptions(),
    ...defaults,
  });

  const allowGroupsEl = document.getElementById("opt_ssh_allow_groups");
  if (allowGroupsEl && !String(allowGroupsEl.value || "").trim()) {
    allowGroupsEl.value = "sudo";
  }
  updateSdOptionsConfiguredCount();
  sdOptionsProfileStatusEl.textContent = "Security starter defaults applied: SSH password off, root SSH off, fail2ban on, unattended upgrades on. 🔐✨";
}

function renderSdOptionProfiles() {
  const selected = sdOptionsProfileSelectEl.value;
  const known = new Set(Object.keys(sdOptionProfiles));
  const ordered = [];
  for (const name of sdOptionProfileOrder) {
    if (known.has(name) && !ordered.includes(name)) {
      ordered.push(name);
    }
  }
  const names = ordered.concat(Object.keys(sdOptionProfiles).filter((name) => !ordered.includes(name)).sort((a, b) => a.localeCompare(b)));
  sdOptionsProfileSelectEl.innerHTML = "";

  const empty = document.createElement("option");
  empty.value = "";
  empty.textContent = "No saved template selected";
  sdOptionsProfileSelectEl.appendChild(empty);

  for (const name of names) {
    const profile = sdOptionProfiles[name] || {};
    const option = document.createElement("option");
    option.value = name;
    const desc = String(profile.description || "").trim();
    const piBadge = profile.pi_only ? " [Pi]" : "";
    option.textContent = desc ? `${name}${piBadge} - ${desc}` : `${name}${piBadge}`;
    sdOptionsProfileSelectEl.appendChild(option);
  }

  if (selected && names.includes(selected)) {
    sdOptionsProfileSelectEl.value = selected;
  }
  updateSdOptionActions();
}

async function loadSdOptionProfiles(restoreSession = true) {
  try {
    const res = await fetch("/api/sd/options-profiles");
    const data = await res.json();
    if (!res.ok || !data.ok) {
      sdOptionsProfileStatusEl.textContent = data.error || "Could not load cloud-init templates.";
      return;
    }

    const next = {};
    const nextOrder = [];
    for (const profile of Array.isArray(data.profiles) ? data.profiles : []) {
      const name = String(profile.name || "").trim();
      if (!name) continue;
      next[name] = profile;
      nextOrder.push(name);
    }
    sdOptionProfiles = next;
    sdOptionProfileOrder = nextOrder;
    renderSdOptionProfiles();
    const selected = String(data.selected_profile || "");
    if (restoreSession && selected && sdOptionProfiles[selected]) {
      sdOptionsProfileSelectEl.value = selected;
      await loadSelectedSdOptionProfile(false);
    }
    if (restoreSession) {
      const sshKey = String(data.ssh_key_name || "");
      await refreshSshKeyChoices(sshKey);
      if (sshKey && sshKeySelectEl.value === sshKey) await loadSelectedSshKey(false);
      else if (sshKey) sshKeyStatusEl.textContent = "The session's saved SSH key is no longer available. Select another key.";
    }
  } catch (error) {
    sdOptionsProfileStatusEl.textContent = `Could not restore cloud-init configuration: ${error.message}`;
  }
}

async function loadSelectedSdOptionProfile(retainSelection = true) {
  if (retainSelection) await sdBrowserSessionReady;
  const name = String(sdOptionsProfileSelectEl.value || "").trim();
  if (!name) {
    return;
  }
  const profile = sdOptionProfiles[name];
  if (!profile) {
    sdOptionsProfileStatusEl.textContent = "Selected template not found.";
    return;
  }
  const isPiOnlyProfile = Boolean(profile.pi_only);
  if (SD_FIXED_STANDARD_PROFILES.has(profile.profile) || ["uefi-x86_64", "uefi-arm64"].includes(profile.profile)) {
    form.profile.value = profile.profile;
    form.profile.dispatchEvent(new Event("change"));
    if (isUefiInstaller() && profile.catalog_id) {
      form.distro_family.value = "";
      form.distro_release.value = "";
      form.image_search.value = "";
      rebuildCatalogFilters();
      form.os_preset.value = profile.catalog_id;
      applyOsPreset();
    }
  }
  const profileDistribution = String(profile.distribution || "").trim().toLowerCase();
  if (!isPiOnlyProfile && (profileDistribution === "k3s" || profileDistribution === "standard")) {
    form.distribution.value = profileDistribution;
  }
  const currentOptions = gatherCloudInitOptions();
  const profileOptions = profile.options && typeof profile.options === "object" ? profile.options : {};
  applyCloudInitOptions(isPiOnlyProfile ? { ...currentOptions, ...profileOptions } : profileOptions);
  if (sdOptionsProfilePiOnlyEl) {
    sdOptionsProfilePiOnlyEl.checked = isPiOnlyProfile;
  }
  updatePiScopedOptionsVisibility();
  ensureK3sConfigDefaults();
  sdOptionsProfileNameEl.value = name;
  sdOptionsProfileDescriptionEl.value = String(profile.description || "");
  sdOptionsProfileStatusEl.textContent = isPiOnlyProfile
    ? `Loaded Pi-specific template '${name}' and merged it into the current optional settings.`
    : `Loaded template '${name}'.`;
  updateSdOptionActions();
  if (retainSelection) {
    try {
      await saveSdBrowserSelection({name});
    } catch (error) {
      sdOptionsProfileStatusEl.textContent = `Template loaded, but session selection could not be retained: ${error.message}`;
    }
  }
}

async function saveCurrentSdOptionProfile() {
  await sdBrowserSessionReady;
  const name = String(sdOptionsProfileNameEl.value || "").trim();
  if (!name) {
    sdOptionsProfileStatusEl.textContent = "Enter a template name before saving.";
    return;
  }
  const payload = {
    name,
    description: String(sdOptionsProfileDescriptionEl.value || "").trim(),
    distribution: String(form.distribution.value || "standard").trim().toLowerCase() || "standard",
    pi_only: Boolean(sdOptionsProfilePiOnlyEl && sdOptionsProfilePiOnlyEl.checked),
    options: gatherCloudInitOptions(),
    secret_source_profile: String(sdOptionsProfileSelectEl.value || ""),
    clear_secret_options: [...sdOptionsInputEls]
      .filter(el => SD_SECRET_KEYS.has(el.dataset.optionKey) && el.dataset.savedSecret === "1" && !el.value)
      .map(el => el.dataset.optionKey),
  };
  if (SD_FIXED_STANDARD_PROFILES.has(form.profile.value)) payload.profile = form.profile.value;
  if (isUefiInstaller()) {
    payload.profile = form.profile.value;
    payload.workflow = "uefi-installer";
    payload.catalog_id = form.os_preset.value;
    payload.pi_only = false;
  }
  try {
    const res = await fetch("/api/sd/options-profiles", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      sdOptionsProfileStatusEl.textContent = data.error || "Could not save template.";
      return;
    }

    const savedName = String((data.profile || {}).name || name).trim();
    if (savedName) {
      sdOptionProfiles[savedName] = data.profile;
      sdOptionProfileOrder = [savedName].concat(sdOptionProfileOrder.filter((name) => name !== savedName));
      renderSdOptionProfiles();
      sdOptionsProfileSelectEl.value = savedName;
      sdOptionsProfileNameEl.value = savedName;
      for (const el of sdOptionsInputEls) {
        const key = el.dataset.optionKey;
        if (!SD_SECRET_KEYS.has(key) || el.disabled || !optionScopeMatches(el)) continue;
        const value = data.profile.options[key];
        if (value === SD_SAVED_SECRET) el.value = value;
        el.dataset.savedSecret = value === SD_SAVED_SECRET ? "1" : "";
        updateSdSavedSecretHint(el);
      }
    }
    sdOptionsProfileStatusEl.textContent = `Saved template '${savedName || name}'.`;
    updateSdOptionsConfiguredCount();
    updateSdOptionActions();
  } catch (error) {
    sdOptionsProfileStatusEl.textContent = `Could not save template: ${error.message}`;
  }
}

async function deleteSelectedSdOptionProfile() {
  const name = String(sdOptionsProfileSelectEl.value || "").trim();
  if (!name) {
    sdOptionsProfileStatusEl.textContent = "Select a template to delete.";
    return;
  }
  if (!confirm(`Delete template '${name}'?`)) {
    return;
  }

  const res = await fetch(`/api/sd/options-profiles/${encodeURIComponent(name)}`, { method: "DELETE" });
  const data = await res.json();
  if (!res.ok || !data.ok) {
    sdOptionsProfileStatusEl.textContent = data.error || "Could not delete template.";
    return;
  }

  delete sdOptionProfiles[name];
  sdOptionProfileOrder = sdOptionProfileOrder.filter((item) => item !== name);
  renderSdOptionProfiles();
  sdOptionsProfileNameEl.value = "";
  sdOptionsProfileDescriptionEl.value = "";
  for (const el of sdOptionsInputEls) {
    if (!SD_SECRET_KEYS.has(el.dataset.optionKey) || el.value !== SD_SAVED_SECRET) continue;
    el.value = "";
    el.dataset.savedSecret = "";
    updateSdSavedSecretHint(el);
  }
  sdOptionsProfileStatusEl.textContent = `Deleted template '${name}'.`;
  updateSdOptionActions();
}
