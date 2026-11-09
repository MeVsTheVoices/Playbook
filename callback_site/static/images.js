(() => {
  const body = document.body;
  const toggleBtn = document.getElementById("sidebar-toggle");
  const imageCatalogBodyEl = document.getElementById("image-catalog-body");
  const managedImagesBodyEl = document.getElementById("managed-images-body");
  const imageRecordStatusEl = document.getElementById("image-record-status");
  const imageRecordIdEl = document.getElementById("image_record_id");
  const imageLabelEl = document.getElementById("image_label");
  const imageProfileEl = document.getElementById("image_profile");
  const imageSourceEl = document.getElementById("image_source");
  const fallbackPasswordEl = document.getElementById("fallback_password");
  const imageSourceValueEl = document.getElementById("image_source_value");
  const imageDownloadPathEl = document.getElementById("image_download_path");
  const imageHashStatusEl = document.getElementById("image_hash_status");
  const imageExpectedShaEl = document.getElementById("image_expected_sha");
  const imageObservedShaEl = document.getElementById("image_observed_sha");
  const imageJobStatusEl = document.getElementById("image_job_status");
  const imageExitCodeEl = document.getElementById("image_exit_code");
  const imageNotesEl = document.getElementById("image_notes");
  const imageCatalog = Array.isArray(window.__IMAGE_CATALOG__) ? window.__IMAGE_CATALOG__ : [];
  const catalogPullSelectEl = document.getElementById("catalog_pull_ids");
  const catalogPullSkipExistingEl = document.getElementById("catalog_pull_skip_existing");
  const catalogPullStatusEl = document.getElementById("catalog-pull-status");
  const catalogPullLogEl = document.getElementById("catalog-pull-log");
  const catalogPullCardEl = document.getElementById("catalog-pull-card");
  const catalogPullBadgeEl = document.getElementById("catalog-pull-badge");
  const catalogPullMetaEl = document.getElementById("catalog-pull-meta");
  const catalogPullTimingEl = document.getElementById("catalog-pull-timing");
  const catalogPullSubstatusEl = document.getElementById("catalog-pull-substatus");
  const catalogPullProgressWrapEl = document.getElementById("catalog-pull-progress-wrap");
  const catalogPullProgressFillEl = document.getElementById("catalog-pull-progress-fill");
  const catalogPullProgressLabelEl = document.getElementById("catalog-pull-progress-label");
  const catalogPullProgressTextEl = document.getElementById("catalog-pull-progress-text");
  const catalogPullProgressMetaEl = document.getElementById("catalog-pull-progress-meta");
  const catalogAvailabilityBodyEl = document.getElementById("catalog-availability-body");
  const catalogAvailabilityStatusEl = document.getElementById("catalog-availability-status");
  const catalogAvailabilityWrapEl = document.getElementById("catalog-availability-wrap");
  const catalogGroupedListEl = document.getElementById("catalog-grouped-list");
  const toggleCatalogCompactBtn = document.getElementById("toggle-catalog-compact");
  const catalogSourceCountEl = document.getElementById("catalog-source-count");
  const catalogSourceSummaryEl = document.getElementById("catalog-source-summary");
  const catalogSourcePanelEl = document.getElementById("catalog-source-panel");
  const cloudInitProfilesContainerEl = document.getElementById("cloud-init-profiles-container");
  const cloudInitLoadStatusEl = document.getElementById("cloud-init-load-status");

  let currentCatalogPullJob = null;
  let catalogRefreshTimer = null;
  let managedImageRecords = [];
  let catalogEntriesCache = [];

  function pullAllMissingCatalogEntries() {
    const ids = catalogEntriesCache
      .filter((entry) => !entry.local_available && String(entry.image_url || "").trim())
      .map((entry) => String(entry.id || ""));
    if (!ids.length) {
      catalogPullStatusEl.textContent = "Nothing missing: every downloadable preset is already cached.";
      return;
    }
    startCatalogPullForIds(ids, `${ids.length} missing preset${ids.length === 1 ? "" : "s"}`);
  }

  function availabilityGlyph(available) {
    return available ? "✅" : "❌";
  }

  function availabilityLabel(available) {
    return available ? "downloaded" : "missing";
  }

  function setCurrentCatalogPullJob(job) {
    currentCatalogPullJob = job || null;
    window.__catalogPullJob = currentCatalogPullJob;
  }

  function catalogPullPercent(summary) {
    const total = Number(summary?.total || 0);
    if (!total) return 0;
    const finished = Number(summary?.ok || 0) + Number(summary?.failed || 0) + Number(summary?.skipped || 0);
    return Math.max(0, Math.min(100, Math.round((finished / total) * 100)));
  }

  function formatBytes(value) {
    const bytes = Number(value || 0);
    if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let size = bytes;
    let idx = 0;
    while (size >= 1024 && idx < units.length - 1) {
      size /= 1024;
      idx += 1;
    }
    const digits = size >= 100 || idx === 0 ? 0 : size >= 10 ? 1 : 2;
    return `${size.toFixed(digits)} ${units[idx]}`;
  }

  function catalogPullStateText(status) {
    if (status === "running" || status === "queued") return "Pulling images now";
    if (status === "completed" || status === "completed_with_errors") return "Pull complete";
    if (status === "failed") return "Pull failed";
    return "Waiting for a pull";
  }

  function availabilityStateFromJob(job, entryId) {
    const results = Array.isArray(job?.results) ? job.results : [];
    const match = results.find((item) => String(item?.id || "") === String(entryId || ""));
    if (!match) return null;
    if (match.status === "downloading") return "pulling";
    if (match.status === "ok" || match.status === "skipped") return "pulled";
    if (match.status === "failed") return "failed";
    return null;
  }

  function updateCatalogPullTiles(job) {
    const activeIds = new Set(Array.isArray(job?.results) ? job.results.filter((item) => item.status === "downloading").map((item) => String(item.id || "")) : []);
    document.querySelectorAll(".catalog-group-item[data-catalog-id]").forEach((item) => {
      const entryId = String(item.dataset.catalogId || "");
      const pullState = availabilityStateFromJob(job, entryId);
      item.classList.toggle("pulling", pullState === "pulling");
      item.classList.toggle("pulled", pullState === "pulled");
      item.classList.toggle("failed", pullState === "failed");

      const stateEl = item.querySelector("[data-role='catalog-state']");
      const buttonEl = item.querySelector("button[data-role='catalog-pull-button']");
      const emoji = pullState === "pulling" ? "⏳" : (pullState === "pulled" ? "😊" : (pullState === "failed" ? "😵" : "🙂"));
      const label = pullState === "pulling" ? "Pulling..." : (pullState === "pulled" ? "Pulled" : (pullState === "failed" ? "Needs attention" : "Ready to pull"));
      if (stateEl) {
        stateEl.className = `catalog-group-item-status${pullState ? ` ${pullState}` : ""}`;
        stateEl.innerHTML = `<span class="status-dot"></span><span>${emoji}</span><span>${label}</span>`;
      }
      if (buttonEl) {
        buttonEl.disabled = pullState === "pulling";
        buttonEl.textContent = pullState === "pulling" ? "Pulling..." : (item.classList.contains("pulled") ? "Pull again" : "Pull");
      }
    });
  }

  function setCatalogAvailabilityCompact(compact, persist = true) {
    if (!catalogAvailabilityWrapEl || !toggleCatalogCompactBtn) return;
    catalogAvailabilityWrapEl.classList.toggle("compact", !!compact);
    toggleCatalogCompactBtn.textContent = compact ? "Expand availability" : "Shrink availability";
    if (persist) {
      localStorage.setItem("catalog-availability-compact", compact ? "1" : "0");
    }
  }

  function formatJobTiming(job) {
    if (!job || !job.created_at) return "";
    const createdMs = new Date(job.created_at).getTime();
    const finishedMs = job.finished_at ? new Date(job.finished_at).getTime() : Date.now();
    const durationSec = Math.max(0, Math.round((finishedMs - createdMs) / 1000));
    const duration = durationSec > 60 ? `${Math.floor(durationSec / 60)}m ${durationSec % 60}s` : `${durationSec}s`;
    return `Duration: ${duration}${job.status === "running" ? " (ongoing)" : ""}`;
  }

  function setPullCardState(job) {
    setCurrentCatalogPullJob(job);
    const status = String(job?.status || "idle").toLowerCase();
    const badgeClass = status === "running" ? "running" : (status === "completed" || status === "completed_with_errors" ? "completed" : (status === "failed" ? "failed" : ""));
    const emoji = status === "running" ? "🙂⏳" : (status === "completed" || status === "completed_with_errors" ? "😄✅" : (status === "failed" ? "😵🔥" : "🧊"));
    const summaryText = status === "running"
      ? "Downloading selected presets right now."
      : (status === "completed" || status === "completed_with_errors"
        ? "Bulk pull finished. Refresh availability if the cache just changed."
        : (status === "failed"
          ? "Bulk pull failed. Check the log below for details."
          : "No download job running."));
    const metaText = status === "running"
      ? `Job ${job?.id || "-"} is active.`
      : (status === "completed" || status === "completed_with_errors" || status === "failed"
        ? `Job ${job?.id || "-"} ${status.replace(/_/g, " ")}.`
        : "Pick presets above, then start a bulk pull.");

    if (catalogPullBadgeEl) {
      catalogPullBadgeEl.className = `job-status-badge ${badgeClass}`.trim();
      catalogPullBadgeEl.innerHTML = `<span class="status-dot"></span> ${emoji} ${status.toUpperCase()}`;
    }
    if (catalogPullCardEl) {
      catalogPullCardEl.classList.toggle("pulling", status === "running" || status === "queued");
      catalogPullCardEl.classList.toggle("completed", status === "completed" || status === "completed_with_errors");
      catalogPullCardEl.classList.toggle("failed", status === "failed");
    }
    if (catalogPullMetaEl) {
      catalogPullMetaEl.textContent = metaText;
    }
    if (catalogPullTimingEl) {
      catalogPullTimingEl.textContent = formatJobTiming(job);
    }
    if (catalogPullSubstatusEl) {
      catalogPullSubstatusEl.textContent = summaryText;
    }
    if (catalogPullProgressWrapEl) {
      catalogPullProgressWrapEl.classList.toggle("active", status === "running" || status === "queued");
      catalogPullProgressWrapEl.classList.toggle("complete", status === "completed" || status === "completed_with_errors");
      catalogPullProgressWrapEl.classList.toggle("failed", status === "failed");
    }

    const summary = job?.summary || {};
    const total = Number(summary?.total || 0);
    const finished = Number(summary?.ok || 0) + Number(summary?.failed || 0) + Number(summary?.skipped || 0);
    const running = Number(summary?.running || 0);
    const results = Array.isArray(job?.results) ? job.results : [];
    const activeResult = results.find((item) => String(item?.status || "") === "downloading") || null;
    const activeDownloaded = Number(activeResult?.bytes_downloaded || 0);
    const activeTotal = Number(activeResult?.bytes_total || 0);
    const activePercent = Number(activeResult?.progress_percent || 0);
    const percent = status === "running" && activePercent > 0 ? activePercent : catalogPullPercent(summary);
    if (catalogPullProgressLabelEl) {
      catalogPullProgressLabelEl.textContent = catalogPullStateText(status);
    }
    if (catalogPullProgressTextEl) {
      const bytesText = activeResult
        ? activeTotal > 0
          ? `${formatBytes(activeDownloaded)} / ${formatBytes(activeTotal)}`
          : `${formatBytes(activeDownloaded)} downloaded`
        : "";
      const summaryText = total ? `${finished} / ${total} complete${running ? ` • ${running} active` : ""}` : "0 / 0 complete";
      catalogPullProgressTextEl.textContent = bytesText ? `${summaryText} • ${bytesText}` : summaryText;
    }
    if (catalogPullProgressFillEl) {
      catalogPullProgressFillEl.style.width = `${percent}%`;
    }
    if (catalogPullProgressMetaEl) {
      if (status === "running" || status === "queued") {
        if (activeResult) {
          const detail = activeTotal > 0
            ? `${formatBytes(activeDownloaded)} of ${formatBytes(activeTotal)} transferred (${Math.max(0, Math.min(100, activePercent))}%).`
            : `${formatBytes(activeDownloaded)} transferred so far.`;
          catalogPullProgressMetaEl.innerHTML = `<span>Download in progress.</span><span>${detail}</span>`;
        } else {
          catalogPullProgressMetaEl.innerHTML = `<span>Download in progress.</span><span>Waiting for transfer metrics.</span>`;
        }
      } else if (status === "completed" || status === "completed_with_errors") {
        catalogPullProgressMetaEl.innerHTML = `<span>Download complete.</span><span>Refresh the catalog if you want the tiles to relabel immediately.</span>`;
      } else if (status === "failed") {
        catalogPullProgressMetaEl.innerHTML = `<span>Download failed.</span><span>Check the log and try again.</span>`;
      } else {
        catalogPullProgressMetaEl.innerHTML = `<span>Idle.</span><span>Start a pull to begin.</span>`;
      }
    }
    updateCatalogPullTiles(job);
  }

  function initSidebar() {
    const saved = localStorage.getItem("sidebar-collapsed");
    if (saved === "1") body.classList.add("sidebar-collapsed");
  }

  function updateSourceOptions() {
    const profile = String(imageProfileEl.value || "").trim();
    const filtered = profile ? imageCatalog.filter((entry) => (entry.profiles || []).includes(profile)) : [];
    imageSourceEl.innerHTML = `<option value="">Select release/track...</option>`;
    filtered.forEach((entry) => {
      const option = document.createElement("option");
      option.value = entry.id;
      option.textContent = `${entry.release} (${entry.distro}, ${entry.arch})`;
      imageSourceEl.appendChild(option);
    });
  }

  function clearImageRecordForm() {
    imageRecordIdEl.value = "";
    imageLabelEl.value = "";
    imageProfileEl.value = "";
    imageSourceEl.value = "";
    fallbackPasswordEl.value = "";
    imageSourceValueEl.value = "";
    imageDownloadPathEl.value = "";
    imageHashStatusEl.value = "";
    imageExpectedShaEl.value = "";
    imageObservedShaEl.value = "";
    imageJobStatusEl.value = "";
    imageExitCodeEl.value = "";
    imageNotesEl.value = "";
    imageRecordStatusEl.textContent = "Form cleared. Add a record or edit an existing row.";
  }

  function normalizeManagedImage(record) {
    return {
      id: record.id || "",
      recorded_at: record.recorded_at || "-",
      profile: record.profile || "-",
      label: record.label || record.source_value || record.download_path || "Retrieved image",
      source: record.source || "-",
      source_value: record.source_value || record.download_path || "-",
      hash_status: record.hash_status || "-",
      job_status: record.job_status || "-",
      exit_code: record.exit_code,
      notes: record.notes || "",
      download_path: record.download_path || "",
      expected_sha256: record.expected_sha256 || "",
      observed_sha256: record.observed_sha256 || "",
      local_available: !!record.local_available,
    };
  }

  async function deleteManagedImage(recordId, label) {
    if (!confirm(`Delete image record '${label}'?`)) {
      return;
    }
    imageRecordStatusEl.textContent = "Deleting record...";
    const res = await fetch(`/api/sd/images/${encodeURIComponent(recordId)}`, { method: "DELETE" });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      imageRecordStatusEl.textContent = data.error || "Failed to delete image record.";
      return;
    }
    imageRecordStatusEl.textContent = "Image record deleted.";
    clearImageRecordForm();
    loadManagedImages();
  }

  window.deleteManagedImage = deleteManagedImage;

  function fillImageRecordForm(record) {
    imageRecordIdEl.value = record.id || "";
    imageLabelEl.value = record.label || "";
    imageProfileEl.value = record.profile || "";
    updateSourceOptions();
    imageSourceEl.value = record.source || "";
    fallbackPasswordEl.value = record.fallback_password || "";
    imageSourceValueEl.value = record.source_value || "";
    imageDownloadPathEl.value = record.download_path || "";
    imageHashStatusEl.value = record.hash_status || "";
    imageExpectedShaEl.value = record.expected_sha256 || "";
    imageObservedShaEl.value = record.observed_sha256 || "";
    imageJobStatusEl.value = record.job_status || "";
    imageExitCodeEl.value = record.exit_code === null || record.exit_code === undefined ? "" : String(record.exit_code);
    imageNotesEl.value = record.notes || "";
    imageRecordStatusEl.textContent = `Editing record ${record.id}. Update fields and save.`;
  }

  window.editManagedImage = (recordId) => {
    const record = managedImageRecords.find(row => String(row.id) === String(recordId));
    if (!record) {
      imageRecordStatusEl.textContent = "This record is not loaded. Refresh the image records and try again.";
      return;
    }
    fillImageRecordForm(normalizeManagedImage(record));
  };

  async function loadManagedImages() {
    try {
      const [res, fragment] = await Promise.all([
        fetch("/api/sd/images"),
        fetch("/fragments/sd/images-rows"),
      ]);
      const data = await res.json();
      if (!res.ok || !data.ok || !Array.isArray(data.entries)) {
        throw new Error(data.error || "Invalid managed image records response.");
      }
      if (!fragment.ok) {
        throw new Error(`Image cards returned HTTP ${fragment.status}.`);
      }
      const cards = await fragment.text();
      managedImageRecords = data.entries;
      managedImagesBodyEl.innerHTML = cards;
    } catch (error) {
      imageRecordStatusEl.textContent = `Failed to load managed image records: ${error.message}`;
    }
  }

  async function saveImageRecord() {
    const payload = {
      label: imageLabelEl.value.trim(),
      profile: imageProfileEl.value,
      source: imageSourceEl.value.trim(),
      source_value: imageSourceValueEl.value.trim(),
      download_path: imageDownloadPathEl.value.trim(),
      hash_status: imageHashStatusEl.value.trim(),
      expected_sha256: imageExpectedShaEl.value.trim(),
      observed_sha256: imageObservedShaEl.value.trim(),
      job_status: imageJobStatusEl.value.trim(),
      exit_code: imageExitCodeEl.value.trim() === "" ? null : Number(imageExitCodeEl.value),
      fallback_password: fallbackPasswordEl.value.trim(),
      notes: imageNotesEl.value.trim(),
    };

    const recordId = imageRecordIdEl.value.trim();
    if (!payload.label) {
      imageRecordStatusEl.textContent = "Label is required to save a record.";
      return;
    }

    imageRecordStatusEl.textContent = recordId ? "Updating record..." : "Creating record...";
    const res = await fetch(recordId ? `/api/sd/images/${encodeURIComponent(recordId)}` : "/api/sd/images", {
      method: recordId ? "PUT" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      imageRecordStatusEl.textContent = data.error || "Failed to save image record.";
      return;
    }

    imageRecordStatusEl.textContent = recordId ? "Image record updated." : "Image record created.";
    clearImageRecordForm();
    loadManagedImages();
  }

  async function loadImageCatalog() {
    try {
      const res = await fetch("/fragments/sd/image-catalog-rows");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      imageCatalogBodyEl.innerHTML = await res.text();
    } catch (error) {
      imageCatalogBodyEl.textContent = `Could not refresh missing presets: ${error.message}`;
    }
  }

  function rebuildCatalogPullOptions(catalogEntries) {
    catalogPullSelectEl.innerHTML = "";
    const entries = catalogEntries.filter((entry) => String(entry.image_url || "").trim()).sort((a, b) => String(a.label || "").localeCompare(String(b.label || "")));

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
      option.textContent = `${entry.label} ${availabilityGlyph(entry.local_available)} ${availabilityLabel(entry.local_available)}`;
      catalogPullSelectEl.appendChild(option);
    }
  }

  function renderGroupedCatalogEntries(entries) {
    if (!catalogGroupedListEl) return;
    const available = entries.filter((entry) => entry.local_available);
    const missing = entries.filter((entry) => !entry.local_available);
    const groups = [
      { key: "missing", title: "Missing from cache", subtitle: "These are the ones you can safely ignore for now while you focus on what is already present.", items: missing, open: false },
      { key: "downloaded", title: "Already downloaded", subtitle: "These are the presets you can use immediately.", items: available, open: true },
    ];

    catalogGroupedListEl.innerHTML = "";
    for (const group of groups) {
      const details = document.createElement("details");
      details.className = "catalog-group-card";
      details.open = group.open;

      const summary = document.createElement("summary");
      summary.innerHTML = `
        <div>
          <div class="catalog-group-title">${group.title}</div>
          <div class="catalog-group-subtitle">${group.subtitle}</div>
        </div>
        <span class="tag ${group.items.length ? "" : "tag-muted"}">${group.items.length} preset${group.items.length === 1 ? "" : "s"}</span>
      `;

      const body = document.createElement("div");
      body.className = "catalog-group-body";
      if (!group.items.length) {
        const empty = document.createElement("div");
        empty.className = "hint";
        empty.textContent = `No ${group.key === "missing" ? "missing" : "downloaded"} presets right now.`;
        body.appendChild(empty);
      } else {
        const toolbar = document.createElement("div");
        toolbar.className = "catalog-group-toolbar";
        const selectAllLabel = document.createElement("label");
        selectAllLabel.className = "catalog-select-all";
        const selectAll = document.createElement("input");
        selectAll.type = "checkbox";
        selectAllLabel.appendChild(selectAll);
        selectAllLabel.appendChild(document.createTextNode(" Select all"));
        const bulkBtn = document.createElement("button");
        bulkBtn.type = "button";
        bulkBtn.className = "catalog-inline-btn";
        bulkBtn.disabled = true;
        bulkBtn.textContent = "Pull selected (0)";
        toolbar.appendChild(selectAllLabel);
        toolbar.appendChild(bulkBtn);
        body.appendChild(toolbar);

        const checkedBoxes = () => Array.from(body.querySelectorAll("input.catalog-select:checked"));
        const refreshToolbar = () => {
          const boxes = Array.from(body.querySelectorAll("input.catalog-select:not(:disabled)"));
          const checked = checkedBoxes().length;
          bulkBtn.disabled = checked === 0;
          bulkBtn.textContent = `Pull selected (${checked})`;
          selectAll.checked = boxes.length > 0 && checked === boxes.length;
        };
        selectAll.addEventListener("change", () => {
          body.querySelectorAll("input.catalog-select:not(:disabled)").forEach((box) => { box.checked = selectAll.checked; });
          refreshToolbar();
        });
        bulkBtn.addEventListener("click", (event) => {
          event.preventDefault();
          const ids = checkedBoxes().map((box) => String(box.value || ""));
          startCatalogPullForIds(ids, `${ids.length} selected preset${ids.length === 1 ? "" : "s"}`);
        });

        for (const entry of group.items) {
          const item = document.createElement("div");
          item.className = "catalog-group-item";
          item.dataset.catalogId = entry.id || "";
          const pick = document.createElement("input");
          pick.type = "checkbox";
          pick.className = "catalog-select";
          pick.value = entry.id || "";
          pick.disabled = !String(entry.image_url || "").trim();
          pick.title = pick.disabled ? "No download URL for this preset" : "Select for bulk pull";
          pick.addEventListener("change", refreshToolbar);
          item.appendChild(pick);
          const main = document.createElement("div");
          main.className = "catalog-group-item-main";
          main.style.flex = "1";
          const title = document.createElement("div");
          title.className = "catalog-group-item-title";
          title.textContent = entry.label || entry.id || "Catalog entry";
          const meta = document.createElement("div");
          meta.className = "catalog-group-item-meta";
          meta.textContent = `${entry.id || "-"}${entry.image_url ? ` • ${entry.image_url}` : ""}`;
          main.appendChild(title);
          main.appendChild(meta);
          const state = document.createElement("div");
          state.dataset.role = "catalog-state";
          state.className = `catalog-group-item-status ${group.key === "missing" ? "" : "pulled"}`.trim();
          state.innerHTML = `<span class="status-dot"></span><span>${group.key === "missing" ? "🙂" : "😊"}</span><span>${group.key === "missing" ? "Ready to pull" : "Cached"}</span>`;
          main.appendChild(state);
          const actions = document.createElement("div");
          actions.className = "catalog-group-item-actions";
          const pullBtn = document.createElement("button");
          pullBtn.type = "button";
          pullBtn.className = "catalog-inline-btn";
          pullBtn.dataset.role = "catalog-pull-button";
          pullBtn.textContent = group.key === "missing" ? "Pull" : "Pull again";
          pullBtn.addEventListener("click", (event) => {
            event.preventDefault();
            event.stopPropagation();
            startCatalogPullForIds([entry.id], entry.label || entry.id);
          });
          actions.appendChild(pullBtn);
          item.appendChild(main);
          item.appendChild(actions);
          body.appendChild(item);
        }
      }
      details.appendChild(summary);
      details.appendChild(body);
      catalogGroupedListEl.appendChild(details);
    }
    updateCatalogPullTiles(currentCatalogPullJob);
  }

  async function loadCatalogAvailability() {
    catalogAvailabilityStatusEl.textContent = "Loading availability...";
    const res = await fetch("/api/sd/catalog");
    const data = await res.json();
    if (!res.ok || !data.ok) {
      catalogAvailabilityStatusEl.textContent = data.error || "Failed to load catalog availability.";
      return;
    }

    const entries = Array.isArray(data.entries) ? data.entries : [];
    catalogEntriesCache = entries;
    catalogAvailabilityBodyEl.innerHTML = "";
    if (!entries.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 5;
      td.textContent = "No catalog entries.";
      tr.appendChild(td);
      catalogAvailabilityBodyEl.appendChild(tr);
    } else {
      for (const entry of entries) {
        const tr = document.createElement("tr");
        const cols = [entry.id || "-", entry.label || "-", entry.image_url || "-", entry.local_available ? "available" : "missing", entry.cache_path || "-"];
        cols.forEach((value, idx) => {
          const td = document.createElement("td");
          if (idx === 2 || idx === 4) {
            const code = document.createElement("code");
            if (idx === 4) {
              code.className = entry.local_available ? "cache-path-ok" : "cache-path-missing";
            }
            code.textContent = value;
            td.appendChild(code);
          } else if (idx === 3) {
            const tag = document.createElement("span");
            tag.className = entry.local_available ? "tag" : "tag-muted";
            tag.textContent = value;
            td.appendChild(tag);
          } else {
            td.textContent = value;
          }
          tr.appendChild(td);
        });
        catalogAvailabilityBodyEl.appendChild(tr);
      }
    }

    renderGroupedCatalogEntries(entries);
    rebuildCatalogPullOptions(entries);
    const availableCount = entries.filter((entry) => entry.local_available).length;
    const missingCount = Math.max(0, entries.length - availableCount);
    catalogAvailabilityStatusEl.textContent = `Loaded ${entries.length} catalog entries (${availableCount} downloaded, ${missingCount} missing).`;
    if (catalogSourceCountEl) {
      catalogSourceCountEl.innerHTML = `<span class="status-dot"></span><span>${availableCount} downloaded / ${missingCount} missing</span>`;
    }
    if (catalogSourceSummaryEl) {
      catalogSourceSummaryEl.textContent = `Distro image presets and bulk pulls. ${availableCount} downloaded, ${missingCount} missing.`;
    }
  }

  function renderCatalogPullJob(job) {
    setCurrentCatalogPullJob(job);
    const summary = job.summary || {};
    setPullCardState(job);
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
        lines.push(`- ${item.id || "?"}: ${item.status || "unknown"} | ${item.message || ""} | ${item.download_path || ""}`);
      }
    }
    catalogPullLogEl.textContent = lines.join("\n");
  }

  async function pollCatalogPullJob() {
    if (!currentCatalogPullJob) return;
    const res = await fetch(`/api/sd/catalog-pull/jobs/${currentCatalogPullJob.id}`);
    const data = await res.json();
    if (!res.ok || !data.ok) {
      catalogPullStatusEl.textContent = data.error || "Failed to load catalog pull status.";
      return;
    }

    renderCatalogPullJob(data.job);
    if (data.job.status === "queued" || data.job.status === "running") {
      setTimeout(pollCatalogPullJob, 1300);
    } else {
      loadCatalogAvailability();
      loadImageCatalog();
      loadManagedImages();
    }
  }

  async function startCatalogPullForIds(catalogIds, label = "selected presets") {
    const selectedIds = Array.isArray(catalogIds) ? catalogIds.filter(Boolean) : [];
    if (!selectedIds.length) {
      catalogPullStatusEl.textContent = "Select at least one catalog entry to pull.";
      return;
    }

    catalogPullStatusEl.textContent = `Submitting pull job for ${label}...`;
    catalogPullLogEl.textContent = "";
    const res = await fetch("/api/sd/catalog-pull", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ catalog_ids: selectedIds, skip_existing: catalogPullSkipExistingEl.checked }),
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

    currentCatalogPullJob = {
      id: data.job_id,
      status: "running",
      created_at: new Date().toISOString(),
      summary: { ok: 0, failed: 0, skipped: 0, running: selectedIds.length, total: selectedIds.length },
      results: [],
    };
    setCurrentCatalogPullJob(currentCatalogPullJob);
    catalogPullStatusEl.textContent = `Pull job started for ${label}: ${data.job_id}`;
    setPullCardState(currentCatalogPullJob);
    pollCatalogPullJob();
  }

  async function startCatalogPull() {
    const selectedIds = Array.from(catalogPullSelectEl.selectedOptions).map((option) => String(option.value || "").trim()).filter(Boolean);
    if (!selectedIds.length) {
      catalogPullStatusEl.textContent = "Select at least one catalog entry to pull.";
      return;
    }
    await startCatalogPullForIds(selectedIds, "selected presets");
  }

  async function loadCloudInitProfiles() {
    cloudInitLoadStatusEl.textContent = "Loading profiles...";
    const res = await fetch("/api/sd/options-profiles");
    const data = await res.json();
    if (!res.ok || !data.ok) {
      cloudInitLoadStatusEl.textContent = data.error || "Failed to load cloud-init profiles.";
      return;
    }
    const profiles = Array.isArray(data.profiles) ? data.profiles : [];
    cloudInitProfilesContainerEl.innerHTML = "";

    if (!profiles.length) {
      const emptyDiv = document.createElement("div");
      emptyDiv.className = "panel";
      emptyDiv.style.padding = "0.8rem";
      emptyDiv.innerHTML = "<p class='hint'>No cloud-init option profiles configured yet. Create one via /api/sd/options-profiles POST.</p>";
      cloudInitProfilesContainerEl.appendChild(emptyDiv);
      cloudInitLoadStatusEl.textContent = "No profiles found.";
      return;
    }

    for (const profile of profiles) {
      const profileCard = document.createElement("div");
      profileCard.className = "panel";
      profileCard.style.padding = "0.85rem";

      const nameEl = document.createElement("h3");
      nameEl.style.margin = "0 0 0.4rem";
      nameEl.textContent = profile.name || "(unnamed)";
      profileCard.appendChild(nameEl);

      if (profile.pi_only) {
        const badge = document.createElement("span");
        badge.className = "catalog-pill available";
        badge.textContent = "Pi-only";
        profileCard.appendChild(badge);
      }

      if (profile.description) {
        const descEl = document.createElement("p");
        descEl.className = "hint";
        descEl.textContent = profile.description;
        profileCard.appendChild(descEl);
      }

      const updatedEl = document.createElement("p");
      updatedEl.className = "hint";
      updatedEl.style.fontSize = "0.75rem";
      const dateStr = profile.updated_at ? new Date(profile.updated_at).toLocaleString() : "unknown";
      updatedEl.textContent = `Updated: ${dateStr}`;
      profileCard.appendChild(updatedEl);

      const optionsEl = document.createElement("pre");
      optionsEl.className = "job-log";
      optionsEl.dataset.format = "json";
      optionsEl.style.maxHeight = "150px";
      optionsEl.style.marginTop = "0.5rem";
      optionsEl.textContent = JSON.stringify(profile.options || {}, null, 2);
      profileCard.appendChild(optionsEl);

      cloudInitProfilesContainerEl.appendChild(profileCard);
    }

    cloudInitLoadStatusEl.textContent = `Loaded ${profiles.length} profile(s).`;
  }

  function startCatalogRefreshTimer() {
    if (catalogRefreshTimer) {
      clearInterval(catalogRefreshTimer);
    }
    catalogRefreshTimer = setInterval(() => {
      loadCatalogAvailability();
      loadImageCatalog();
      loadCloudInitProfiles();
      if (!currentCatalogPullJob) {
        setPullCardState(null);
      }
    }, 15000);
  }

  function boot() {
    initSidebar();
    const storedCompactState = localStorage.getItem("catalog-availability-compact");
    setCatalogAvailabilityCompact(storedCompactState === null ? true : storedCompactState === "1", false);
    loadCatalogAvailability();
    loadImageCatalog();
    loadManagedImages();
    loadCloudInitProfiles();
    startCatalogRefreshTimer();
    document.querySelectorAll('a[href="#catalog-source-panel"]').forEach(link => {
      link.addEventListener("click", () => { catalogSourcePanelEl.open = true; });
    });

    document.getElementById("save-image-record").addEventListener("click", saveImageRecord);
    document.getElementById("clear-image-record").addEventListener("click", clearImageRecordForm);
    document.getElementById("start-catalog-pull").addEventListener("click", startCatalogPull);
    document.getElementById("pull-all-missing").addEventListener("click", pullAllMissingCatalogEntries);
    document.getElementById("refresh-catalog-availability").addEventListener("click", loadCatalogAvailability);
    document.getElementById("refresh-cloud-init-profiles").addEventListener("click", loadCloudInitProfiles);

    if (toggleCatalogCompactBtn) {
      toggleCatalogCompactBtn.addEventListener("click", () => {
        const compact = !catalogAvailabilityWrapEl.classList.contains("compact");
        setCatalogAvailabilityCompact(compact);
      });
    }
    imageProfileEl.addEventListener("change", updateSourceOptions);

    if (toggleBtn) {
      toggleBtn.addEventListener("click", () => {
        body.classList.toggle("sidebar-collapsed");
        localStorage.setItem("sidebar-collapsed", body.classList.contains("sidebar-collapsed") ? "1" : "0");
      });
    }

    if (catalogSourcePanelEl) {
      const saved = localStorage.getItem("image-source-panel-open");
      if (saved === "0") {
        catalogSourcePanelEl.open = false;
      }
      catalogSourcePanelEl.addEventListener("toggle", () => {
        localStorage.setItem("image-source-panel-open", catalogSourcePanelEl.open ? "1" : "0");
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }
})();
