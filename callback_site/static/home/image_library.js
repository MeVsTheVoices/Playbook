function formatCatalogHash(row) {
  return row.observed_sha256 || row.expected_sha256 || "-";
}

function renderImageCatalog(entries) {
  imageCatalogBodyEl.innerHTML = "";
  if (!entries || !entries.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 6;
    td.textContent = "No image catalog entries yet.";
    tr.appendChild(td);
    imageCatalogBodyEl.appendChild(tr);
    return;
  }

  for (const row of entries) {
    const tr = document.createElement("tr");
    const cols = [
      row.recorded_at || "-",
      row.profile || "-",
      row.source_value || row.source || "-",
      row.hash_status || "-",
      formatCatalogHash(row),
      `${row.job_status || "-"} (${row.exit_code === null || row.exit_code === undefined ? "-" : row.exit_code})`,
    ];
    cols.forEach((value, idx) => {
      const td = document.createElement("td");
      if (idx === 2 || idx === 4) {
        const code = document.createElement("code");
        code.textContent = value;
        td.appendChild(code);
      } else {
        td.textContent = value;
      }
      tr.appendChild(td);
    });
    imageCatalogBodyEl.appendChild(tr);
  }
}

async function loadImageCatalog() {
  try {
    const res = await fetch("/api/sd/image-catalog");
    const data = await res.json();
    if (!res.ok || !data.ok) {
      return;
    }
    renderImageCatalog(data.entries || []);
  } catch (_) {
    // Keep existing table content if refresh fails.
  }
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
  };
}

function clearImageRecordForm() {
  imageRecordIdEl.value = "";
  imageLabelEl.value = "";
  imageProfileEl.value = "";
  imageSourceEl.value = "";
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

function fillImageRecordForm(record) {
  imageRecordIdEl.value = record.id || "";
  imageLabelEl.value = record.label || "";
  imageProfileEl.value = record.profile || "";
  imageSourceEl.value = record.source || "";
  imageSourceValueEl.value = record.source_value || "";
  imageDownloadPathEl.value = record.download_path || "";
  imageHashStatusEl.value = record.hash_status || "";
  imageExpectedShaEl.value = record.expected_sha256 || "";
  imageObservedShaEl.value = record.observed_sha256 || "";
  imageJobStatusEl.value = record.job_status || "";
  imageExitCodeEl.value = record.exit_code === null || record.exit_code === undefined ? "" : String(record.exit_code);
  imageNotesEl.value = record.notes || "";
  imageRecordStatusEl.textContent = `Editing record ${record.id}. Update fields and save.`;
  document.getElementById("retrieved-image-manager").scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderManagedImages(entries) {
  managedImagesBodyEl.innerHTML = "";
  if (!entries || !entries.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 8;
    td.textContent = "No managed image records yet.";
    tr.appendChild(td);
    managedImagesBodyEl.appendChild(tr);
    return;
  }

  for (const row of entries) {
    const normalized = normalizeManagedImage(row);
    const tr = document.createElement("tr");
    const cols = [
      normalized.recorded_at,
      normalized.label,
      normalized.profile,
      normalized.source_value,
      normalized.hash_status,
      `${normalized.job_status} (${normalized.exit_code === null || normalized.exit_code === undefined ? "-" : normalized.exit_code})`,
      normalized.notes,
    ];

    cols.forEach((value, idx) => {
      const td = document.createElement("td");
      if (idx === 1 || idx === 3 || idx === 6) {
        const code = document.createElement("code");
        code.textContent = value || "-";
        td.appendChild(code);
      } else {
        td.textContent = value || "-";
      }
      tr.appendChild(td);
    });

    const actionTd = document.createElement("td");
    const actionWrap = document.createElement("div");
    actionWrap.className = "row-actions";

    const editBtn = document.createElement("button");
    editBtn.type = "button";
    editBtn.textContent = "Edit";
    editBtn.addEventListener("click", () => fillImageRecordForm(normalized));

    const deleteBtn = document.createElement("button");
    deleteBtn.type = "button";
    deleteBtn.textContent = "Delete";
    deleteBtn.className = "danger-btn";
    deleteBtn.addEventListener("click", () => deleteManagedImage(normalized.id, normalized.label));

    actionWrap.appendChild(editBtn);
    actionWrap.appendChild(deleteBtn);
    actionTd.appendChild(actionWrap);
    tr.appendChild(actionTd);
    managedImagesBodyEl.appendChild(tr);
  }
}

async function loadManagedImages() {
  try {
    const res = await fetch("/api/sd/images");
    const data = await res.json();
    if (!res.ok || !data.ok) {
      imageRecordStatusEl.textContent = "Failed to load managed image records.";
      return;
    }
    managedImageRecords = Array.isArray(data.entries) ? data.entries : [];
    renderManagedImages(managedImageRecords);
  } catch (_) {
    imageRecordStatusEl.textContent = "Failed to load managed image records.";
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
