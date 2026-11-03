function rebuildCatalogPullOptions() {
  catalogPullSelectEl.innerHTML = "";
  const entries = imageCatalog
    .filter((entry) => String(entry.image_url || "").trim())
    .sort((a, b) => String(a.label || "").localeCompare(String(b.label || "")));

  if (!entries.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "No downloadable catalog entries available";
    catalogPullSelectEl.appendChild(option);
    catalogPullSelectEl.disabled = true;
    return;
  }

  catalogPullSelectEl.disabled = false;
  for (const entry of entries) {
    const option = document.createElement("option");
    option.value = entry.id;
    option.textContent = `${entry.label} (${entry.id})`;
    catalogPullSelectEl.appendChild(option);
  }
}

function renderCatalogPullJob(job) {
  const summary = job.summary || {};
  catalogPullStatusEl.textContent = `Catalog pull ${job.id}: ${job.status}`;
  const lines = [
    `storage_dir: ${job.storage_dir || "n/a"}`,
    `accepted: ${(job.accepted_ids || []).join(", ") || "(none)"}`,
    `summary: ok=${summary.ok || 0} failed=${summary.failed || 0} skipped=${summary.skipped || 0} total=${summary.total || 0}`,
    "",
    "results:",
  ];

  const results = Array.isArray(job.results) ? job.results : [];
  if (!results.length) {
    lines.push(job.status === "completed" || job.status === "completed_with_errors" ? "(no results returned)" : "(waiting for worker progress)");
  } else {
    for (const item of results) {
      lines.push(
        `- ${item.id || "?"}: ${item.status || "unknown"} | ${item.message || ""} | ${item.download_path || ""}`
      );
    }
  }
  catalogPullLogEl.innerHTML = highlightLogText(lines.join("\n"));
}

async function pollCatalogPullJob() {
  if (!currentCatalogPullJob) return;
  const res = await fetch(`/api/sd/catalog-pull/jobs/${currentCatalogPullJob}`);
  const data = await res.json();
  if (!res.ok || !data.ok) {
    catalogPullStatusEl.textContent = data.error || "Failed to load catalog pull status.";
    return;
  }

  renderCatalogPullJob(data.job);
  if (data.job.status === "queued" || data.job.status === "running") {
    setTimeout(pollCatalogPullJob, 1300);
  } else {
    loadImageCatalog();
    loadManagedImages();
  }
}

async function startCatalogPull() {
  const selectedIds = Array.from(catalogPullSelectEl.selectedOptions)
    .map((option) => String(option.value || "").trim())
    .filter(Boolean);

  if (!selectedIds.length) {
    catalogPullStatusEl.textContent = "Select at least one catalog entry to pull.";
    return;
  }

  catalogPullStatusEl.textContent = "Submitting bulk pull job...";
  catalogPullLogEl.textContent = "";
  const res = await fetch("/api/sd/catalog-pull", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      catalog_ids: selectedIds,
      skip_existing: catalogPullSkipExistingEl.checked,
    }),
  });
  const data = await res.json();
  if (!res.ok || !data.ok) {
    const messages = [data.error || "Failed to start catalog pull job."];
    if (Array.isArray(data.missing_ids) && data.missing_ids.length) {
      messages.push(`missing_ids: ${data.missing_ids.join(", ")}`);
    }
    if (Array.isArray(data.missing_url_ids) && data.missing_url_ids.length) {
      messages.push(`missing_url_ids: ${data.missing_url_ids.join(", ")}`);
    }
    catalogPullStatusEl.textContent = messages.join(" | ");
    return;
  }

  currentCatalogPullJob = data.job_id;
  catalogPullStatusEl.textContent = `Bulk pull job started: ${data.job_id}`;
  pollCatalogPullJob();
}
