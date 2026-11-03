function renderNodeSelectors() {
  const currentCertNode = certNodeEl.value;
  const currentSummaryNode = monitorPingNodeSelectEl.value;
  certNodeEl.innerHTML = "";
  monitorPingNodeSelectEl.innerHTML = "";
  if (!nodes.length) {
    const emptyCert = document.createElement("option");
    emptyCert.value = "";
    emptyCert.textContent = "No nodes available";
    certNodeEl.appendChild(emptyCert);

    const emptyPing = document.createElement("option");
    emptyPing.value = "";
    emptyPing.textContent = "No nodes available";
    monitorPingNodeSelectEl.appendChild(emptyPing);
    summaryRunPingBtn.disabled = true;
    return;
  }

  for (const node of nodes) {
    const opt = document.createElement("option");
    opt.value = node.name;
    opt.textContent = node.name;
    certNodeEl.appendChild(opt);

    const pingOpt = document.createElement("option");
    pingOpt.value = node.name;
    pingOpt.textContent = `${node.name} (${node.group || "node"})`;
    monitorPingNodeSelectEl.appendChild(pingOpt);
  }
  if (currentCertNode && nodes.some((n) => n.name === currentCertNode)) {
    certNodeEl.value = currentCertNode;
  }
  if (currentSummaryNode && nodes.some((n) => n.name === currentSummaryNode)) {
    monitorPingNodeSelectEl.value = currentSummaryNode;
  } else {
    monitorPingNodeSelectEl.value = nodes[0].name;
  }
  summaryRunPingBtn.disabled = false;
}

function renderMonitorNodeSelector() {
  const currentSelection = monitorNodeSelectEl.value;
  const discoveredNodes = nodes.filter((node) => node.source === "callback" || node.source === "callback+managed");
  monitorNodeSelectEl.innerHTML = "";
  if (!discoveredNodes.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "No discovered nodes yet";
    monitorNodeSelectEl.appendChild(opt);
    monitorRunCheckBtn.disabled = true;
    return;
  }

  for (const node of discoveredNodes) {
    const opt = document.createElement("option");
    opt.value = node.name;
    opt.textContent = `${node.name} (${node.group || "discovered"})`;
    monitorNodeSelectEl.appendChild(opt);
  }

  if (currentSelection && discoveredNodes.some((n) => n.name === currentSelection)) {
    monitorNodeSelectEl.value = currentSelection;
  } else {
    monitorNodeSelectEl.value = discoveredNodes[0].name;
  }
  monitorRunCheckBtn.disabled = false;
}
