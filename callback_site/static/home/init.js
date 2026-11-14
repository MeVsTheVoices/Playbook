const imageCachedAtEl = document.getElementById("image-cached-at");
if (imageCachedAtEl) {
  imageCachedAtEl.textContent = new Intl.DateTimeFormat(undefined, {
    dateStyle: "long", timeStyle: "short",
  }).format(new Date(imageCachedAtEl.dateTime));
}

toggleBtn.addEventListener("click", () => {
  body.classList.toggle("sidebar-collapsed");
  localStorage.setItem("sidebar-collapsed", body.classList.contains("sidebar-collapsed") ? "1" : "0");
});

document.getElementById("payload-inspect-clear").addEventListener("click", () => {
  inspectedEventId = null;
  inspectedSnapshot = null;
  payloadInspectMeta.textContent = "Select a callback row to inspect its full payload here. This pane stays put during auto-refresh.";
  payloadInspectJson.textContent = "No payload selected yet.";
  setTerminalHost("");
  eventsBodyEl.querySelectorAll("tr.event-row-selected").forEach((r) => r.classList.remove("event-row-selected"));
});

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  await sdBrowserSessionReady;
  if (isUefiInstaller() && deviceBackend !== "helper") {
    statusEl.textContent = "UEFI installer creation requires a configured Linux flashing helper.";
    return;
  }
  const devicesAvailable = await pollDevicesSnapshot();
  if (!devicesAvailable) {
    statusEl.textContent = lastDeviceRefreshError
      || "Could not verify the current target device. Refresh discovery and try again.";
    flashMascotEl.classList.remove("active");
    return;
  }
  if (!String(form.device.value || "").trim()) {
    statusEl.textContent = "No target SD device selected. Insert a card and try again.";
    flashMascotEl.classList.remove("active");
    return;
  }
  const helperConfirmation = deviceBackend === "helper" ? confirmHelperTarget() : null;
  if (deviceBackend === "helper" && !helperConfirmation) {
    form.confirm_overwrite.checked = false;
    flashMascotEl.classList.remove("active");
    return;
  }
  const installer = isUefiInstaller();
  if (installer && (!form.os_preset.value || !window.confirm(
    "Authorize unattended installation?\n\nBooting this USB will ERASE the sole eligible internal disk "
    + "on the target machine WITHOUT another installation prompt. Existing unmounted partitions do not protect data. "
    + "Zero or multiple eligible disks stop installation.\n\n"
    + "The machine powers off when finished. Remove this USB before restarting. "
    + "Keep this authorized USB physically controlled.\n\nCreate this destructive installer USB?"
  ))) {
    statusEl.textContent = "Installer creation cancelled or no pinned catalog image selected. No device was written.";
    return;
  }
  statusEl.textContent = "Submitting...";
  logEl.textContent = "";
  flashMascotEl.classList.add("active");
  updateSdActivityFromJob({ status: "queued", stdout: "", stderr: "" });
  ensureK3sConfigDefaults();
  const payload = {
    profile: form.profile.value,
    distribution: form.distribution.value,
    hostname: form.hostname.value,
    device: form.device.value,
    callback_url: form.callback_url.value,
    image_url: form.image_url.value,
    image_file: form.image_file.value,
    image_sha256: form.image_sha256.value,
    workspace: form.workspace.value,
    ssh_public_key: form.ssh_public_key.value,
    ssh_key_name: form.ssh_public_key.dataset.keyName || "",
    sd_options_profile: sdOptionsProfileSelectEl.value,
    pi_only: Boolean(sdOptionsProfilePiOnlyEl && sdOptionsProfilePiOnlyEl.checked),
    cloud_init_options: gatherCloudInitOptions(),
    workflow: installer ? "uefi-installer" : "pi",
    catalog_id: form.os_preset.value,
    ...(installer ? { install_authorized: true, image_file: "", workspace: "", pi_only: false } : {}),
    ...(helperConfirmation || {}),
  };

  const res = await fetch("/api/sd/prepare", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (!res.ok || !data.ok) {
    flashMascotEl.classList.remove("active");
    statusEl.textContent = (data.errors || ["Failed to start job"]).join("; ");
    return;
  }
  currentJob = data.job_id;
  localStorage.setItem(SD_ACTIVE_JOB_KEY, currentJob);
  statusEl.textContent = `Job started: ${currentJob}`;
  pollJob();
});

document.getElementById("generate-ssh-key").addEventListener("click", () => runSshKeyAction("generate"));

document.getElementById("kload-ssh-key").addEventListener("click", () => runSshKeyAction("kload"));

document.getElementById("load-selected-ssh-key").addEventListener("click", () => loadSelectedSshKey());

document.getElementById("refresh-ssh-key-list").addEventListener("click", () => refreshSshKeyChoices());
document.getElementById("regenerate-ssh-key").addEventListener("click", () => toggleSshKeyRegeneration(true));
document.getElementById("cancel-regenerate-ssh-key").addEventListener("click", () => toggleSshKeyRegeneration(false));
document.getElementById("confirm-regenerate-ssh-key").addEventListener("click", regenerateSelectedSshKey);
document.getElementById("ssh_key_new_name").addEventListener("keydown", event => {
  if (event.key === "Enter") {
    event.preventDefault();
    regenerateSelectedSshKey();
  } else if (event.key === "Escape") {
    toggleSshKeyRegeneration(false);
  }
});

document.getElementById("clear-ssh-key-file").addEventListener("click", () => {
  sshKeyUploadEl.value = "";
  sshKeyStatusEl.textContent = "Cleared uploaded key file.";
});

sshKeySelectEl.addEventListener("change", () => {
  toggleSshKeyRegeneration(false);
  document.getElementById("regenerate-ssh-key").disabled = !sshKeySelectEl.value;
  refreshKeyLinks(String(sshKeySelectEl.value || ""));
});

sshKeyUploadEl.addEventListener("change", handleSshKeyUpload);
form.ssh_public_key.addEventListener("input", () => {
  const wasSavedKey = Boolean(form.ssh_public_key.dataset.keyName);
  delete form.ssh_public_key.dataset.keyName;
  if (wasSavedKey) {
    saveSdBrowserSelection({ssh_key_name: ""}).catch(error => {
      sshKeyStatusEl.textContent = `Could not clear saved session key selection: ${error.message}`;
    });
  }
});

sdOptionsSaveBtn.addEventListener("click", saveCurrentSdOptionProfile);

sdOptionsResetBtn.addEventListener("click", resetCloudInitOptions);

sdOptionsSecurityDefaultsBtn.addEventListener("click", applySecurityStarterDefaults);

sdOptionsDeleteBtn.addEventListener("click", deleteSelectedSdOptionProfile);

sdOptionsProfileSelectEl.addEventListener("change", () => {
  const name = String(sdOptionsProfileSelectEl.value || "").trim();
  if (name && sdOptionProfiles[name]) {
    loadSelectedSdOptionProfile();
  } else {
    saveSdBrowserSelection({name: ""}).catch(error => {
      sdOptionsProfileStatusEl.textContent = `Could not clear saved session template selection: ${error.message}`;
    });
  }
  updateSdOptionActions();
});

for (const el of sdOptionsInputEls) {
  const eventName = String(el.dataset.optionType || "") === "checkbox" ? "change" : "input";
  el.addEventListener(eventName, updateSdOptionsConfiguredCount);
  if (SD_SECRET_KEYS.has(el.dataset.optionKey)) {
    const hint = document.createElement("p");
    hint.id = `${el.id}-saved-hint`;
    hint.className = "small-note";
    el.insertAdjacentElement("afterend", hint);
    el.addEventListener("input", () => updateSdSavedSecretHint(el));
  }
}

document.getElementById("opt_k3s_role")?.addEventListener("change", () => {
  updateK3sRoleHints();
  updateOsScopedOptionsVisibility();
});

document.getElementById("ssh_key_name").addEventListener("input", refreshKeyLinks);

form.profile.addEventListener("change", () => {
  ensureK3sConfigDefaults();
  form.image_sha256.value = "";
  rebuildCatalogFilters();
  updatePiScopedOptionsVisibility();
  applyK3sRoleDefaults();
  updateOsScopedOptionsVisibility();
  updateSdOptionsConfiguredCount();
  updateInstallerWorkflow();
});

document.getElementById("pi_model_select").addEventListener("change", (event) => {
  form.profile.value = event.target.value;
  form.profile.dispatchEvent(new Event("change"));
});

form.distribution.addEventListener("change", ensureK3sConfigDefaults);

form.distribution.addEventListener("change", updateOsScopedOptionsVisibility);

form.distro_family.addEventListener("change", rebuildCatalogFilters);

form.distro_release.addEventListener("change", rebuildPresetOptions);

form.image_search.addEventListener("input", rebuildPresetOptions);

form.os_preset.addEventListener("change", applyOsPreset);

if (sdOptionsProfilePiOnlyEl) {
  sdOptionsProfilePiOnlyEl.addEventListener("change", updatePiScopedOptionsVisibility);
}

eventSearchEl.addEventListener("input", () => {
  const visibleEvents = filterEvents(allEvents);
  updateEventStats(allEvents, visibleEvents);
  renderEvents(visibleEvents);
});

eventProfileEl.addEventListener("change", () => {
  const visibleEvents = filterEvents(allEvents);
  updateEventStats(allEvents, visibleEvents);
  renderEvents(visibleEvents);
});

eventStageEl.addEventListener("change", () => {
  const visibleEvents = filterEvents(allEvents);
  updateEventStats(allEvents, visibleEvents);
  renderEvents(visibleEvents);
});

initSidebar();

if (eventsToggleOlderBtn) {
  eventsToggleOlderBtn.addEventListener("click", toggleOlderEvents);
}

ensureK3sConfigDefaults();

rebuildCatalogFilters();

applyK3sRoleDefaults();

updateOsScopedOptionsVisibility();

updatePiScopedOptionsVisibility();

refreshKeyLinks();

renderSdOptionProfiles();

resumeActiveJobIfAny();

updateSdActivityFromJob(null);

updateCallbackActivity([]);

updateDeviceActivity(0, false);

updateSdOptionsConfiguredCount();

sdBrowserSessionReady = loadSdOptionProfiles();

startDeviceFallbackPolling();

fetch("/api/sd/backend").then(async (response) => {
  if (!response.ok) throw new Error("Could not read flashing backend capabilities.");
  updateDeviceDiscoveryMode((await response.json()).backend);
}).catch((error) => {
  statusEl.textContent = error.message;
});

pollDevicesSnapshot().then(() => {
  if (deviceBackend === "native") {
    startDeviceWebhook();
  }
});

loadEvents();

setInterval(loadEvents, 5000);
