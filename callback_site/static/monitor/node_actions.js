async function saveNode() {
  const payload = {
    name: document.getElementById("node-name").value.trim(),
    group: document.getElementById("node-group").value.trim(),
    address: document.getElementById("node-address").value.trim(),
    enabled: document.getElementById("node-enabled").value === "true",
    notes: document.getElementById("node-notes").value.trim(),
  };
  nodeFormStatusEl.textContent = "Saving...";
  const res = await fetch("/api/monitor/nodes", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (!res.ok || !data.ok) {
    nodeFormStatusEl.textContent = data.error || "Failed to save node.";
    return;
  }
  nodeFormStatusEl.textContent = `Saved ${data.node.name}.`;
  await loadSummary();
}

async function pullCertificate() {
  const nodeName = certNodeEl.value;
  const pullCertBtn = document.getElementById("pull-cert");
  if (!pullCertBtn) {
    return;
  }

  const existingTimer = pingResetTimers.get(pullCertBtn);
  if (existingTimer) {
    clearTimeout(existingTimer);
    pingResetTimers.delete(pullCertBtn);
  }

  if (!nodeName) {
    certStatusEl.textContent = "Choose a node first.";
    finishPingButton(pullCertBtn, "ping-warn", "Pick Node");
    return;
  }
  pullCertBtn.disabled = true;
  setPingVisualState(pullCertBtn, "ping-pending", "Pulling");
  certStatusEl.textContent = `Pulling certificate for ${nodeName}...`;
  try {
    const res = await fetch(`/api/monitor/nodes/${encodeURIComponent(nodeName)}/certificate`);
    const data = await res.json();
    if (!res.ok || !data.ok) {
      certStatusEl.textContent = data.error || "Failed to pull certificate.";
      finishPingButton(pullCertBtn, "ping-bad", "Bad");
      return;
    }
    const cert = data.certificate || {};
    certOutputEl.textContent = [
      `node: ${nodeName}`,
      `received_at: ${cert.received_at || "-"}`,
      `cert_present: ${cert.cert_present ? "true" : "false"}`,
      `cert_subject: ${cert.cert_subject || "-"}`,
      `cert_issuer: ${cert.cert_issuer || "-"}`,
      `cert_fingerprint: ${cert.cert_fingerprint || "-"}`,
      `source_ip: ${cert.source_ip || "-"}`,
    ].join("\n");
    certStatusEl.textContent = `Certificate info loaded for ${nodeName}.`;
    finishPingButton(pullCertBtn, cert.cert_present ? "ping-good" : "ping-warn", cert.cert_present ? "Good" : "Slow");
  } catch (err) {
    certStatusEl.textContent = `Failed to pull certificate: ${String(err && err.message ? err.message : err)}`;
    finishPingButton(pullCertBtn, "ping-bad", "Bad");
  } finally {
    if (!pingResetTimers.has(pullCertBtn)) {
      resetPingButton(pullCertBtn);
    }
  }
}
