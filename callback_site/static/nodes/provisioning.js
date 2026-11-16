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
          lines.push(`  ${item.profile_id || "profile"}:`);
          lines.push(output.split("\n").map((line) => `    ${line}`).join("\n"));
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

async function pollProvisionJob(jobId, mode, packageOperation = false) {
  const deadline = Date.now() + (packageOperation ? 4800000 : mode === "hardware" ? 180000 : 120000);
  const label = mode === "hardware" ? "Hardware profile" : mode === "check" ? "Check" : "Apply";
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
          ? `${label} finished, but one or more profiles need attention.`
          : `${label} finished successfully.`;
        provisionLog.textContent = formatProvisionResult({ results });
        await loadSummary();
        if (mode === "apply") loadLiveConfig();
        return;
      }
      if (job.status === "failed") {
        provisionStatus.textContent = job.error || `${label} failed.`;
        provisionLog.textContent = formatProvisionResult({ results: job.results || [] });
        await loadSummary();
        return;
      }
      provisionStatus.textContent = mode === "hardware" ? "Getting hardware profile..." : `${mode === "check" ? "Checking" : "Applying"} provisioning in progress...`;
      await new Promise((resolve) => setTimeout(resolve, 1500));
    } catch (err) {
      provisionStatus.textContent = `Provisioning job polling failed: ${String(err && err.message ? err.message : err)}`;
      return;
    }
  }
  provisionStatus.textContent = `${label} is still running in the background; refresh the node summary to see the result.`;
}

async function runHardwareProfile(node) {
  provisionStatus.textContent = `Getting hardware profile for ${node}...`;
  try {
    const res = await fetchJsonWithTimeout("/api/nodes/hardware-check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node }),
    });
    const data = await res.json();
    if (!res.ok || !data.ok || !data.job_id) {
      provisionStatus.textContent = data.error || "Hardware profile request failed.";
      return;
    }
    await pollProvisionJob(data.job_id, "hardware");
  } catch (err) {
    provisionStatus.textContent = `Hardware profile request failed: ${String(err && err.message ? err.message : err)}`;
  }
}

async function runProvision(node, mode, profileIds, packageUpdate = false) {
  provisionStatus.textContent = `${mode === "check" ? "Checking" : "Applying"} provisioning for the selected node...`;
  try {
    const requestBody = { node, mode, run_ansible: runAnsible.checked };
    if (Array.isArray(profileIds) && profileIds.length) {
      requestBody.profile_ids = profileIds;
    }
    if (packageUpdate) {
      requestBody.package_update = true;
      requestBody.confirm_reboot = true;
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
      await pollProvisionJob(data.job_id, mode, profileIds?.includes("package-manager"));
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
