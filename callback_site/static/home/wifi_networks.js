function renderWifiWidget(widget) {
  const field = widget.querySelector("[data-option-key='wifi_networks']");
  const rows = widget.querySelector("[data-wifi-rows]");
  const saved = field.value === SD_SAVED_SECRET;
  rows.replaceChildren();
  widget.querySelector("[data-wifi-saved]").hidden = !saved;
  widget.querySelector("[data-wifi-replace]").hidden = !saved;
  widget.querySelector("[data-wifi-clear]").hidden = !saved;
  widget.querySelector("[data-wifi-add]").hidden = saved;
  if (saved || !field.value) return;
  for (const network of JSON.parse(field.value)) addWifiRow(widget, network);
}

function updateWifiWidget(widget) {
  const networks = Array.from(widget.querySelectorAll("[data-wifi-row]")).map((row) => ({
    ssid: row.querySelector("[data-wifi-ssid]").value,
    password: row.querySelector("[data-wifi-password]").value,
  }));
  const value = networks.length ? JSON.stringify(networks) : "";
  widget.querySelector("[data-option-key='wifi_networks']").value = value;
  document.querySelectorAll("[data-wifi-widget]").forEach((other) => {
    if (other === widget) return;
    other.querySelector("[data-option-key='wifi_networks']").value = value;
    renderWifiWidget(other);
  });
  updateSdOptionsConfiguredCount();
}

function addWifiRow(widget, network = {ssid: "", password: ""}) {
  const row = document.createElement("div");
  row.className = "form-grid";
  row.dataset.wifiRow = "";
  const ssid = document.createElement("input");
  ssid.dataset.wifiSsid = "";
  ssid.setAttribute("aria-label", "Additional Wi-Fi SSID");
  ssid.placeholder = "Wi-Fi SSID";
  ssid.value = network.ssid;
  const password = document.createElement("input");
  password.type = "password";
  password.autocomplete = "off";
  password.dataset.wifiPassword = "";
  password.setAttribute("aria-label", "Additional Wi-Fi password");
  password.placeholder = "Wi-Fi password";
  password.value = network.password;
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "secondary";
  remove.textContent = "Remove network";
  for (const input of [ssid, password]) input.addEventListener("input", () => updateWifiWidget(widget));
  remove.addEventListener("click", () => { row.remove(); updateWifiWidget(widget); });
  row.append(ssid, password, remove);
  widget.querySelector("[data-wifi-rows]").appendChild(row);
}

function initWifiWidgets() {
  document.querySelectorAll("[data-wifi-widget]").forEach((widget) => {
    widget.querySelector("[data-wifi-add]").addEventListener("click", () => {
      addWifiRow(widget);
      updateWifiWidget(widget);
    });
    widget.querySelector("[data-wifi-replace]").addEventListener("click", () => {
      widget.querySelector("[data-option-key='wifi_networks']").value = "";
      renderWifiWidget(widget);
      addWifiRow(widget);
      updateWifiWidget(widget);
    });
    widget.querySelector("[data-wifi-clear]").addEventListener("click", () => {
      widget.querySelector("[data-option-key='wifi_networks']").value = "";
      renderWifiWidget(widget);
      updateWifiWidget(widget);
    });
  });
}
