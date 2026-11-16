function formatDeviceOption(device) {
  const model = device.model || "";
  const size = typeof device.size === "number" ? `${(device.size / 1000000000).toFixed(1)} GB` : (device.size || "");
  const serial = device.serial ? ` · S/N ${device.serial}` : "";
  const transport = device.transport ? ` (${device.transport})` : "";
  return `${device.path} - ${size} - ${model}${serial}${transport}`.trim();
}

function replaceDeviceOptions(devices) {
  const previous = form.device.value;
  const previousOption = form.device.options[form.device.selectedIndex];
  const previousIdentity = previousOption ? previousOption.dataset.identity : "";
  form.device.innerHTML = "";
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = devices.length ? "Select a removable device" : "No removable SD/USB devices detected";
  form.device.appendChild(placeholder);
  for (const device of devices) {
    const option = document.createElement("option");
    option.value = device.path;
    option.textContent = formatDeviceOption(device);
    option.dataset.identity = device.identity;
    form.device.appendChild(option);
  }
  if (previous && devices.some((d) => d.path === previous && d.identity === previousIdentity)) {
    form.device.value = previous;
    return;
  }
  form.device.value = "";
  if (previous) {
    form.confirm_overwrite.checked = false;
    deviceWebhookStatusEl.textContent = "Target removed or changed. Select and confirm a device again.";
  }
}

function validDevices(devices) {
  return Array.isArray(devices) && new Set(devices.map((device) => device?.path)).size === devices.length
    && devices.every((device) => device && typeof device.path === "string" && device.path.startsWith("/dev/")
      && typeof device.identity === "string" && /^[a-f0-9]{64}$/.test(device.identity));
}

async function pollDevicesSnapshot() {
  const sequence = ++deviceRefreshSequence;
  try {
    const res = await fetch("/api/sd/devices");
    const payload = await res.json();
    if (sequence !== deviceRefreshSequence) {
      lastDeviceRefreshError = "Device refresh was superseded. Try again.";
      return false;
    }
    if (!Array.isArray(payload.errors)) throw new Error("Device discovery returned an invalid response.");
    if (!res.ok || payload.errors.length) {
      throw new Error(payload.errors[0] || "Device refresh failed");
    }
    const devices = payload.devices;
    if (!validDevices(devices)) throw new Error("Device discovery returned invalid identity information.");
    deviceProblem = "";
    replaceDeviceOptions(devices);
    lastDeviceRefreshError = "";
    deviceDiscoveryReady = true;
    updateInstallerWorkflow();
    updateDeviceActivity(devices.length, true);
    return true;
  } catch (err) {
    if (sequence !== deviceRefreshSequence) return false;
    lastDeviceRefreshError = String(err.message || err);
    deviceProblem = lastDeviceRefreshError;
    deviceDiscoveryReady = false;
    replaceDeviceOptions([]);
    form.confirm_overwrite.checked = false;
    updateInstallerWorkflow();
    deviceWebhookStatusEl.textContent = `Device refresh error: ${lastDeviceRefreshError}`;
    updateDeviceActivity(0, false);
    return false;
  }
}

form.device.addEventListener("change", () => {
  form.confirm_overwrite.checked = false;
  updateInstallerWorkflow();
});

function startDeviceWebhook() {
  if (typeof EventSource === "undefined") {
    deviceWebhookStatusEl.textContent = "Live device monitor unavailable in this browser.";
    return;
  }

  const es = new EventSource("/api/sd/devices/webhook");
  es.addEventListener("devices", (event) => {
    try {
      const payload = JSON.parse(event.data || "{}");
      if (payload.error) throw new Error(String(payload.error));
      const devices = payload.devices;
      if (!validDevices(devices)) throw new Error("Invalid device identity information.");
      replaceDeviceOptions(devices);
      updateInstallerWorkflow();
      const updated = payload.updated_at ? payload.updated_at.replace("T", " ").replace("+00:00", " UTC") : "just now";
      deviceWebhookStatusEl.textContent = deviceProblem
        ? `SD/USB devices unavailable: ${deviceProblem}`
        : `Live device monitor: ${devices.length} device(s), updated ${updated}`;
      updateDeviceActivity(devices.length, true, deviceProblem);
    } catch (err) {
      deviceDiscoveryReady = false;
      deviceProblem = String(err.message || err);
      replaceDeviceOptions([]);
      form.confirm_overwrite.checked = false;
      updateInstallerWorkflow();
      deviceWebhookStatusEl.textContent = `Device monitor error: ${deviceProblem}`;
      updateDeviceActivity(0, false);
    }
  });

  es.addEventListener("ping", () => {
    if (!deviceProblem && !deviceWebhookStatusEl.textContent.startsWith("Live device monitor:")) {
      deviceWebhookStatusEl.textContent = "Live device monitor: connected";
    }
  });

  es.onerror = () => {
    es.close();
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
    pollDevicesSnapshot();
  }, 5000);

  window.addEventListener("focus", () => {
    pollDevicesSnapshot();
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      pollDevicesSnapshot();
    }
  });
}
