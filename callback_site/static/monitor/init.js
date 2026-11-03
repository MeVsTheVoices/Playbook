toggleBtn.addEventListener("click", () => {
  body.classList.toggle("sidebar-collapsed");
  localStorage.setItem("sidebar-collapsed", body.classList.contains("sidebar-collapsed") ? "1" : "0");
});

document.getElementById("refresh-summary").addEventListener("click", loadSummary);

if (pruneStaleMonitorBtn) {
  pruneStaleMonitorBtn.addEventListener("click", pruneStaleMonitorNodes);
}

monitorRunCheckBtn.addEventListener("click", runMonitorCloserCheck);

summaryRunPingBtn.addEventListener("click", () =>
  runSimplePing(monitorPingNodeSelectEl.value, summaryRunPingBtn)
);

monitorRunPingBtn.addEventListener("click", () =>
  runSimplePing(monitorNodeSelectEl.value, monitorRunPingBtn)
);

document.getElementById("save-node").addEventListener("click", saveNode);

nodeFormPingBtn.addEventListener("click", () => {
  const requested = document.getElementById("node-name").value.trim();
  runSimplePing(requested, nodeFormPingBtn);
});

document.getElementById("clear-node-form").addEventListener("click", clearNodeForm);

document.getElementById("pull-cert").addEventListener("click", pullCertificate);

initSidebar();

loadSummary();
