function formatProvisionResult(result) {
  const lines = [];
  for (const nodeResult of result.results || []) {
    const summary = [];
    for (const item of nodeResult.results || []) {
      const label = item.profile_id || "profile";
      const state = item.status || "unknown";
      const exitCode = item.exit_code ?? "-";
      summary.push(`${label}: ${state} (exit ${exitCode})`);
    }

    lines.push(`Node ${nodeResult.node}: ${summary.join(" | ") || "no profile results"}`);
    if (nodeResult.mode) {
      lines.push(`Mode: ${nodeResult.mode}${nodeResult.run_ansible ? " • Ansible enabled" : " • plan only"}`);
    }
    for (const item of nodeResult.results || []) {
      if (item.output) {
        const output = String(item.output).trim();
        if (output) {
          const shortOutput = output.length > 500 ? `${output.slice(0, 500)}…` : output;
          lines.push(`  ${item.profile_id || "profile"}: ${shortOutput}`);
        }
      }
    }
    lines.push("");
  }
  return lines.join("\n").trim() || "No provisioning output.";
}

async function fetchJsonWithTimeout(url, options = {}, timeoutMs = 20000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

async function pollProvisionJob(jobId, mode) {
  const deadline = Date.now() + 120000;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`/api/nodes/provision/jobs/${encodeURIComponent(jobId)}`);
      const data = await res.json();
      if (!res.ok || !data.ok) {
        provisionStatus.textContent = data.error || "Provisioning job lookup failed.";
        return;
      }
      const job = data.job || {};
      if (job.status === "completed") {
        const results = Array.isArray(job.results) ? job.results : [];
        const hasFailures = results.some((nodeResult) => (nodeResult.results || []).some((item) => item.status === "failed" || item.status === "timeout" || item.status === "unavailable"));
        provisionStatus.textContent = hasFailures
          ? `${mode === "check" ? "Check" : "Apply"} finished, but one or more profiles need attention.`
          : `${mode === "check" ? "Check" : "Apply"} finished successfully.`;
        provisionLog.textContent = formatProvisionResult({ results });
        await loadSummary();
        if (mode === "apply") loadLiveConfig();
        return;
      }
      if (job.status === "failed") {
        provisionStatus.textContent = job.error || `${mode === "check" ? "Check" : "Apply"} failed.`;
        return;
      }
      provisionStatus.textContent = `${mode === "check" ? "Checking" : "Applying"} provisioning in progress...`;
      await new Promise((resolve) => setTimeout(resolve, 1500));
    } catch (err) {
      provisionStatus.textContent = `Provisioning job polling failed: ${String(err && err.message ? err.message : err)}`;
      return;
    }
  }
  provisionStatus.textContent = `${mode === "check" ? "Check" : "Apply"} is still running in the background; refresh the node summary to see the result.`;
}

async function runProvision(node, mode, profileIds) {
  provisionStatus.textContent = `${mode === "check" ? "Checking" : "Applying"} provisioning for the selected node...`;
  try {
    const requestBody = { node, mode, run_ansible: runAnsible.checked };
    if (Array.isArray(profileIds) && profileIds.length) {
      requestBody.profile_ids = profileIds;
    }
    const res = await fetchJsonWithTimeout("/api/nodes/provision", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(requestBody),
    }, 20000);
    const data = await res.json();
    if (!res.ok || !data.ok) {
      if (data.missing_inputs) {
        const messages = [];
        for (const missingNode of data.missing_inputs) {
          for (const profile of missingNode.profiles || []) {
            const fields = (profile.fields || []).map((f) => f.label || f.id).join(", ");
            messages.push(`${missingNode.node} / ${profile.profile_label}: ${fields}`);
          }
        }
        provisionStatus.textContent = `Missing required inputs: ${messages.join(" | ")}`;
      } else {
        provisionStatus.textContent = data.error || "Provisioning request failed.";
      }
      return;
    }

    if (data.job_id) {
      await pollProvisionJob(data.job_id, mode);
      return;
    }

    const hasFailures = (data.results || []).some((nodeResult) => (nodeResult.results || []).some((item) => item.status === "failed" || item.status === "timeout" || item.status === "unavailable"));
    provisionStatus.textContent = hasFailures
      ? `${mode === "check" ? "Check" : "Apply"} finished, but one or more profiles need attention.`
      : `${mode === "check" ? "Check" : "Apply"} finished successfully.`;
    provisionLog.textContent = formatProvisionResult(data);
    await loadSummary();
    if (mode === "apply") loadLiveConfig();
  } catch (err) {
    if (err && err.name === "AbortError") {
      provisionStatus.textContent = `${mode === "check" ? "Check" : "Apply"} is still running in the background; refresh the node summary to see the result.`;
      provisionLog.textContent = `Request exceeded the client timeout while checking ${node || "all nodes"}. The server job may still be completing.`;
      return;
    }
    provisionStatus.textContent = `Provisioning request failed: ${String(err && err.message ? err.message : err)}`;
  }
}
