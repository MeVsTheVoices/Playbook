async function pollMonitorProvisionJob(jobId, nodeName, previouslySeenHardwareAt, triggerButton) {
  const deadline = Date.now() + 120000;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`/api/nodes/provision/jobs/${encodeURIComponent(jobId)}`);
      const data = await res.json();
      if (!res.ok || !data.ok) {
        setMonitorCheckState("error", data.error || "Hardware profile job lookup failed.");
        finishPingButton(triggerButton, "ping-bad", "Bad");
        return;
      }
      const job = data.job || {};
      if (job.status === "completed") {
        await loadSummary();
        const currentNode = nodes.find((item) => item.name === nodeName);
        const currentHardwareAt = currentNode && currentNode.hardware ? currentNode.hardware.received_at : "";
        if (currentHardwareAt && currentHardwareAt !== previouslySeenHardwareAt) {
          setMonitorCheckState("ok", `Hardware callback refreshed for ${nodeName}.`);
          finishPingButton(triggerButton, "ping-good", "Good");
          return;
        }
        const resultEntry = Array.isArray(job.results)
          ? job.results.find((entry) => entry && entry.node === nodeName)
          : null;
        const hardwareTrigger = resultEntry && Array.isArray(resultEntry.results)
          ? resultEntry.results.find((entry) => entry && entry.profile_id === "hardware_check_trigger")
          : null;
        if (hardwareTrigger && hardwareTrigger.status && hardwareTrigger.status !== "ok") {
          const details = String(hardwareTrigger.output || "").trim();
          const shortDetails = details ? ` ${details.slice(0, 220)}` : "";
          setMonitorCheckState("error", `Hardware profile run completed for ${nodeName}, but hardware-check trigger failed (${hardwareTrigger.status}).${shortDetails}`);
          finishPingButton(triggerButton, "ping-bad", "Bad");
          return;
        }
        setMonitorCheckState("error", `Hardware profile run completed for ${nodeName}, but no fresh hardware callback has been seen yet.`);
        finishPingButton(triggerButton, "ping-warn", "Slow");
        return;
      }
      if (job.status === "failed") {
        setMonitorCheckState("error", job.error || `Hardware profile run failed for ${nodeName}.`);
        finishPingButton(triggerButton, "ping-bad", "Bad");
        return;
      }
      setMonitorCheckState("busy", `Triggering hardware profile run for ${nodeName}...`);
      await new Promise((resolve) => setTimeout(resolve, 1500));
    } catch (err) {
      setMonitorCheckState("error", `Hardware profile request failed: ${String(err && err.message ? err.message : err)}`);
      finishPingButton(triggerButton, "ping-bad", "Bad");
      return;
    }
  }
  setMonitorCheckState("error", `The hardware profile run is still running for ${nodeName}; refresh the summary to see if a hardware callback arrives.`);
  finishPingButton(triggerButton, "ping-warn", "Slow");
}

async function runMonitorCloserCheck() {
  const nodeName = monitorNodeSelectEl.value;
  if (!nodeName) {
    setMonitorCheckState("error", "No discovered node is available yet.");
    finishPingButton(monitorRunCheckBtn, "ping-warn", "Pick Node");
    return;
  }

  const existingTimer = pingResetTimers.get(monitorRunCheckBtn);
  if (existingTimer) {
    clearTimeout(existingTimer);
    pingResetTimers.delete(monitorRunCheckBtn);
  }

  const currentNode = nodes.find((item) => item.name === nodeName);
  const previouslySeenHardwareAt = currentNode && currentNode.hardware ? currentNode.hardware.received_at : "";

  monitorRunCheckBtn.disabled = true;
  setPingVisualState(monitorRunCheckBtn, "ping-pending", "Checking");
  setMonitorCheckState("busy", `Triggering hardware profile run for ${nodeName}...`);
  try {
    const res = await fetchJsonWithTimeout("/api/nodes/provision", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node: nodeName, mode: "check", run_ansible: true }),
    }, 20000);
    const data = await res.json();

    if (!res.ok || !data.ok) {
      setMonitorCheckState("error", data.error || "Hardware profile request failed.");
      finishPingButton(monitorRunCheckBtn, "ping-bad", "Bad");
      return;
    }

    if (data.job_id) {
      await pollMonitorProvisionJob(data.job_id, nodeName, previouslySeenHardwareAt, monitorRunCheckBtn);
      return;
    }

    await loadSummary();
    const refreshedNode = nodes.find((item) => item.name === nodeName);
    const refreshedHardwareAt = refreshedNode && refreshedNode.hardware ? refreshedNode.hardware.received_at : "";
    const hasFreshHardware = refreshedHardwareAt && refreshedHardwareAt !== previouslySeenHardwareAt;
    setMonitorCheckState(
      hasFreshHardware ? "ok" : "error",
      hasFreshHardware
        ? `Hardware profile run triggered for ${nodeName}; a fresh hardware callback is visible.`
        : `Hardware profile run submitted for ${nodeName}, but no new hardware callback has been observed yet.`
    );
    finishPingButton(monitorRunCheckBtn, hasFreshHardware ? "ping-good" : "ping-warn", hasFreshHardware ? "Good" : "Slow");
  } catch (err) {
    if (err && err.name === "AbortError") {
      setMonitorCheckState("error", `The hardware profile run is still running for ${nodeName}; refresh the summary to see if a hardware callback arrives.`);
      finishPingButton(monitorRunCheckBtn, "ping-warn", "Slow");
      return;
    }
    setMonitorCheckState("error", `Hardware profile request failed: ${String(err && err.message ? err.message : err)}`);
    finishPingButton(monitorRunCheckBtn, "ping-bad", "Bad");
  } finally {
    if (!pingResetTimers.has(monitorRunCheckBtn)) {
      resetPingButton(monitorRunCheckBtn);
    }
  }
}
