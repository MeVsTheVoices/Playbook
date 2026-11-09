function formatDeviceOption(device) {
  const model = device.model || "";
  const size = device.size || "";
  const serial = device.serial ? ` · S/N ${device.serial}` : "";
  const transport = device.transport ? ` (${device.transport})` : "";
  return `${device.path} - ${size} - ${model}${serial}${transport}`.trim();
}

function stopDeviceWebhook() {
  if (deviceWebhookRetry) {
    clearTimeout(deviceWebhookRetry);
    deviceWebhookRetry = null;
  }
  if (deviceWebhookSource) {
    deviceWebhookSource.close();
    deviceWebhookSource = null;
  }
}

function updateDeviceDiscoveryMode(backend) {
  const previousBackend = deviceBackend;
  const nextBackend = backend === "helper" ? "helper" : "native";
  deviceBackend = nextBackend;
  updateInstallerWorkflow();
  if (deviceBackend === "helper") {
    stopDeviceWebhook();
    deviceWebhookStatusEl.textContent =
      "Flash helper active. Discovery refreshes while this page is visible.";
  } else if (previousBackend === "helper" && deviceWebhookSource === null) {
    startDeviceWebhook();
  }
}

function replaceDeviceOptions(devices) {
  const previous = form.device.value;
  const previousOption = form.device.options[form.device.selectedIndex];
  const previousLabel = previousOption ? previousOption.textContent : previous;
  const previousIdentity = previousOption ? previousOption.dataset.identity || "" : "";
  const previousBackend = deviceOptionsBackend;
  deviceOptionsBackend = deviceBackend;
  currentDevices = devices;
  form.device.innerHTML = "";
  if (!devices.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "No removable SD/USB devices detected";
    form.device.appendChild(option);
    form.device.value = "";
    if (previous || previousBackend !== deviceBackend) {
      form.confirm_overwrite.checked = false;
    }
    deviceWebhookStatusEl.textContent = deviceBackend === "helper"
      ? "Flash helper is online, but no removable SD/USB device is present. Insert one and wait for refresh."
      : "Live device monitor: no removable device present. Reinsert card and wait for refresh.";
    return;
  }

  let selectedDevice = null;
  for (const device of devices) {
    const option = document.createElement("option");
    option.value = device.path;
    option.textContent = formatDeviceOption(device);
    option.dataset.identity = device.identity || "";
    form.device.appendChild(option);
    if (device.path === previous) {
      selectedDevice = device;
    }
  }

  if (selectedDevice) {
    form.device.value = previous;
    const identityChanged = deviceBackend === "helper"
      && previousIdentity
      && previousIdentity !== selectedDevice.identity;
    if (previousBackend !== deviceBackend || identityChanged) {
      form.confirm_overwrite.checked = false;
      deviceWebhookStatusEl.textContent = identityChanged
        ? "Target identity changed. Review the selected device and confirm overwrite again."
        : "Device discovery source changed. Review the selected target and confirm overwrite again.";
    }
    return;
  }

  form.device.value = devices[0].path;
  if (previous || previousBackend !== deviceBackend) {
    form.confirm_overwrite.checked = false;
    deviceWebhookStatusEl.textContent = previous
      ? `Selected device '${previousLabel || previous}' is no longer present. `
        + `Switched to '${devices[0].path}'.`
      : "Device backend changed. Review the selected target and confirm overwrite again.";
  }
}

async function pollDevicesSnapshot() {
  try {
    const res = await fetch("/api/sd/devices");
    const payload = await res.json();
    updateDeviceDiscoveryMode(payload.backend);
    if (!res.ok) {
      throw new Error((payload.errors || ["device refresh failed"])[0]);
    }
    const devices = Array.isArray(payload.devices) ? payload.devices : [];
    replaceDeviceOptions(devices);
    updateDeviceActivity(devices.length, true);
    lastDeviceRefreshError = "";
    return true;
  } catch (err) {
    lastDeviceRefreshError = String(err.message || err);
    if (deviceBackend === "helper") {
      replaceDeviceOptions([]);
    }
    if (!String(deviceWebhookStatusEl.textContent || "").startsWith("Live device monitor disconnected")) {
      deviceWebhookStatusEl.textContent = `Device refresh error: ${lastDeviceRefreshError}`;
    }
    updateDeviceActivity(0, false);
    return false;
  }
}

function startDeviceWebhook() {
  if (deviceBackend === "helper" || deviceWebhookSource) {
    return;
  }
  if (typeof EventSource === "undefined") {
    deviceWebhookStatusEl.textContent = "Live device monitor unavailable in this browser.";
    return;
  }

  const es = new EventSource("/api/sd/devices/webhook");
  deviceWebhookSource = es;
  es.addEventListener("devices", (event) => {
    if (deviceBackend === "helper") {
      stopDeviceWebhook();
      return;
    }
    try {
      const payload = JSON.parse(event.data || "{}");
      const devices = Array.isArray(payload.devices) ? payload.devices : [];
      replaceDeviceOptions(devices);
      const updated = payload.updated_at ? payload.updated_at.replace("T", " ").replace("+00:00", " UTC") : "just now";
      deviceWebhookStatusEl.textContent = `Live device monitor: ${devices.length} device(s), updated ${updated}`;
      updateDeviceActivity(devices.length, true);
    } catch (err) {
      deviceWebhookStatusEl.textContent = "Live device monitor: invalid update payload.";
      updateDeviceActivity(0, false);
    }
  });

  es.addEventListener("ping", () => {
    if (!deviceWebhookStatusEl.textContent.startsWith("Live device monitor:")) {
      deviceWebhookStatusEl.textContent = "Live device monitor: connected";
    }
  });

  es.onerror = () => {
    es.close();
    if (deviceWebhookSource === es) {
      deviceWebhookSource = null;
    }
    if (deviceBackend === "helper") {
      return;
    }
    deviceWebhookStatusEl.textContent = "Live device monitor disconnected. Retrying...";
    updateDeviceActivity(0, false);
    if (deviceWebhookRetry) {
      clearTimeout(deviceWebhookRetry);
    }
    deviceWebhookRetry = setTimeout(startDeviceWebhook, 2500);
  };
}

function startDeviceFallbackPolling() {
  if (deviceFallbackPollTimer) {
    clearInterval(deviceFallbackPollTimer);
  }
  deviceFallbackPollTimer = setInterval(() => {
    if (!document.hidden) {
      pollDevicesSnapshot();
    }
  }, 5000);

  window.addEventListener("focus", () => {
    if (!document.hidden) {
      pollDevicesSnapshot();
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      pollDevicesSnapshot();
    }
  });
}

function confirmHelperTarget() {
  const selected = form.device.options[form.device.selectedIndex];
  const path = String(form.device.value || "").trim();
  const identity = selected ? String(selected.dataset.identity || "") : "";
  if (!path || !identity) {
    statusEl.textContent =
      "The selected target has no verified device identity. Refresh discovery and try again.";
    return null;
  }

  const device = (currentDevices || []).find((item) => item.path === path && item.identity === identity);
  if (!device) {
    statusEl.textContent = "The selected target changed. Refresh discovery and confirm it again.";
    form.confirm_overwrite.checked = false;
    return null;
  }

  const details = [
    `Device: ${device.model || "removable drive"}`,
    `Path: ${path}`,
    `Size: ${device.size || "unknown"}`,
    `Serial: ${device.serial || "not reported"}`,
  ].join("\n");
  const prompt = "Permanently erase and flash this device?\n\n"
    + `${details}\n\nContinue only if this is the intended target.`;
  if (!window.confirm(prompt)) {
    statusEl.textContent = "Flash cancelled. No device was written.";
    return null;
  }
  return { device_identity: identity, confirm_device: path };
}

form.device.addEventListener("change", () => {
  form.confirm_overwrite.checked = false;
});
