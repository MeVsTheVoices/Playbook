toggleBtn.addEventListener("click", () => {
  body.classList.toggle("sidebar-collapsed");
  localStorage.setItem("sidebar-collapsed", body.classList.contains("sidebar-collapsed") ? "1" : "0");
});

provisionNode.addEventListener("change", () => {
  renderModuleSection();
  loadLiveConfig();
});

document.getElementById("module-save-inputs").addEventListener("click", saveModuleInputs);

document.getElementById("module-check").addEventListener("click", () => runProvision(provisionNode.value, "check", selectedModuleId ? [selectedModuleId] : null));

document.getElementById("module-apply").addEventListener("click", () => runProvision(provisionNode.value, "apply", selectedModuleId ? [selectedModuleId] : null));

document.getElementById("save-config").addEventListener("click", saveConfig);

document.getElementById("save-profile-segment").addEventListener("click", saveProfileSegment);

document.getElementById("refresh-profile-segments").addEventListener("click", refreshProfileSegments);

document.getElementById("add-manual-node").addEventListener("click", addManualNode);

document.getElementById("openwrt-add").addEventListener("click", addOpenwrtHost);

document.getElementById("openwrt-probe").addEventListener("click", probeOpenwrtHost);

document.getElementById("openwrt-push-key").addEventListener("click", pushOpenwrtKey);

manualNodeSelectorEl.addEventListener("change", () => {
  const selectedNode = manualNodeSelectorEl.value.trim();
  if (!selectedNode) {
    resetManualNodeForm();
    return;
  }
  fillManualNodeForm(selectedNode);
});

document.getElementById("refresh-certificates").addEventListener("click", refreshCertificates);

document.getElementById("refresh-monitor").addEventListener("click", () => refreshMonitor(false));

document.getElementById("refresh-monitor-webhook").addEventListener("click", () => refreshMonitor(true));

initSidebar();

refreshProfileSegments();

refreshCertificates();

refreshNodeGroups();

loadSummary().then(() => refreshMonitor(false));

setInterval(() => {
  refreshMonitor(false);
}, 15000);
