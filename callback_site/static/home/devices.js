function formatDeviceOption(device) {
  const model = device.model || "";
  const size = device.size || "";
  const transport = device.transport ? ` (${device.transport})` : "";
  return `${device.path} - ${size} - ${model}${transport}`.trim();
}

function replaceDeviceOptions(devices) {
  const previous = form.device.value;
  const previousOption = form.device.options[form.device.selectedIndex];
  const previousLabel = previousOption ? previousOption.textContent : previous;
  form.device.innerHTML = "";
  if (!devices.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "No removable SD/USB devices detected";
    form.device.appendChild(option);
    form.device.value = "";
    deviceWebhookStatusEl.textContent = "Live device monitor: no removable device present. Reinsert card and wait for refresh.";
    return;
  }

  for (const device of devices) {
    const option = document.createElement("option");
    option.value = device.path;
    option.textContent = formatDeviceOption(device);
    form.device.appendChild(option);
  }

  if (previous && devices.some((d) => d.path === previous)) {
    form.device.value = previous;
    return;
  }

  form.device.value = devices[0].path;
  if (previous) {
    deviceWebhookStatusEl.textContent = `Selected device '${previousLabel || previous}' is no longer present. Switched to '${devices[0].path}'.`;
  }
}

async function pollDevicesSnapshot() {
  try {
    const res = await fetch("/api/sd/devices");
    const payload = await res.json();
    if (!res.ok) {
      throw new Error((payload.errors || ["device refresh failed"])[0]);
    }
    const devices = Array.isArray(payload.devices) ? payload.devices : [];
    replaceDeviceOptions(devices);
    updateDeviceActivity(devices.length, true);
  } catch (err) {
    if (!String(deviceWebhookStatusEl.textContent || "").startsWith("Live device monitor disconnected")) {
      deviceWebhookStatusEl.textContent = `Device refresh error: ${String(err.message || err)}`;
    }
    updateDeviceActivity(0, false);
  }
}

function startDeviceWebhook() {
  if (typeof EventSource === "undefined") {
    deviceWebhookStatusEl.textContent = "Live device monitor unavailable in this browser.";
    return;
  }

  const es = new EventSource("/api/sd/devices/webhook");
  es.addEventListener("devices", (event) => {
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
