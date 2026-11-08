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
  await pollDevicesSnapshot();
  if (!String(form.device.value || "").trim()) {
    statusEl.textContent = "No target SD device selected. Insert a card and try again.";
    flashMascotEl.classList.remove("active");
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

document.getElementById("load-selected-ssh-key").addEventListener("click", loadSelectedSshKey);

document.getElementById("refresh-ssh-key-list").addEventListener("click", () => refreshSshKeyChoices());

document.getElementById("clear-ssh-key-file").addEventListener("click", () => {
  sshKeyUploadEl.value = "";
  sshKeyStatusEl.textContent = "Cleared uploaded key file.";
});

sshKeySelectEl.addEventListener("change", () => {
  refreshKeyLinks(String(sshKeySelectEl.value || ""));
});

sshKeyUploadEl.addEventListener("change", handleSshKeyUpload);
form.ssh_public_key.addEventListener("input", () => {
  delete form.ssh_public_key.dataset.keyName;
});

sdOptionsSaveBtn.addEventListener("click", saveCurrentSdOptionProfile);

sdOptionsResetBtn.addEventListener("click", resetCloudInitOptions);

sdOptionsSecurityDefaultsBtn.addEventListener("click", applySecurityStarterDefaults);

sdOptionsDeleteBtn.addEventListener("click", deleteSelectedSdOptionProfile);

sdOptionsProfileSelectEl.addEventListener("change", () => {
  const name = String(sdOptionsProfileSelectEl.value || "").trim();
  if (name && sdOptionProfiles[name]) {
    loadSelectedSdOptionProfile();
  }
  updateSdOptionActions();
});

for (const el of sdOptionsInputEls) {
  const eventName = String(el.dataset.optionType || "") === "checkbox" ? "change" : "input";
  el.addEventListener(eventName, updateSdOptionsConfiguredCount);
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

refreshSshKeyChoices();

loadSdOptionProfiles();

startDeviceWebhook();

startDeviceFallbackPolling();

pollDevicesSnapshot();

loadEvents();

setInterval(loadEvents, 5000);
