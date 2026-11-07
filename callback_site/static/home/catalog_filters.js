function profileArch(profile) {
  return "arm64";
}

function profileCatalog(profile) {
  return imageCatalog.filter((item) => Array.isArray(item.profiles) && item.profiles.includes(profile));
}

function rebuildCatalogFilters() {
  const profile = form.profile.value;
  const entries = profileCatalog(profile);
  const familySelect = form.distro_family;
  const releaseSelect = form.distro_release;

  const prevFamily = familySelect.value;
  const families = [...new Set(entries.map((e) => e.distro))].sort();
  familySelect.innerHTML = "";
  const allFamily = document.createElement("option");
  allFamily.value = "";
  allFamily.textContent = "All distro families";
  familySelect.appendChild(allFamily);
  for (const family of families) {
    const opt = document.createElement("option");
    opt.value = family;
    opt.textContent = family;
    familySelect.appendChild(opt);
  }
  if (prevFamily && families.includes(prevFamily)) {
    familySelect.value = prevFamily;
  }

  const selectedFamily = familySelect.value;
  const familyEntries = selectedFamily ? entries.filter((e) => e.distro === selectedFamily) : entries;
  const prevRelease = releaseSelect.value;
  const releases = [...new Set(familyEntries.map((e) => e.release))].sort();
  releaseSelect.innerHTML = "";
  const allRelease = document.createElement("option");
  allRelease.value = "";
  allRelease.textContent = "All releases";
  releaseSelect.appendChild(allRelease);
  for (const release of releases) {
    const opt = document.createElement("option");
    opt.value = release;
    opt.textContent = release;
    releaseSelect.appendChild(opt);
  }
  if (prevRelease && releases.includes(prevRelease)) {
    releaseSelect.value = prevRelease;
  }

  rebuildPresetOptions();
}

function rebuildPresetOptions() {
  const profile = form.profile.value;
  const family = form.distro_family.value;
  const release = form.distro_release.value;
  const query = (form.image_search.value || "").trim().toLowerCase();
  const select = form.os_preset;
  const previous = select.value;

  let entries = profileCatalog(profile);
  if (family) entries = entries.filter((e) => e.distro === family);
  if (release) entries = entries.filter((e) => e.release === release);
  if (query) {
    entries = entries.filter((e) => {
      const hay = [e.label, e.distro, e.release, e.arch, e.notes].join(" ").toLowerCase();
      return hay.includes(query);
    });
  }

  select.innerHTML = "";
  if (!entries.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "No presets match filters. Use manual URL/file.";
    select.appendChild(opt);
    applyOsPreset();
    return;
  }

  entries.sort((a, b) => {
    if (!!a.local_available !== !!b.local_available) return a.local_available ? -1 : 1;
    if (a.recommended !== b.recommended) return a.recommended ? -1 : 1;
    return a.label.localeCompare(b.label);
  });

  for (const preset of entries) {
    const option = document.createElement("option");
    option.value = preset.id;
    option.textContent = `${preset.local_available ? "✅ Cached" : "⬇️ Pull needed"} • ${preset.label}${preset.recommended ? " [Recommended]" : ""}`;
    option.dataset.imageUrl = preset.image_url || "";
    option.dataset.cachePath = preset.cache_path || "";
    option.dataset.localAvailable = preset.local_available ? "1" : "";
    option.dataset.imageSha256 = preset.image_sha256 || "";
    option.dataset.notes = preset.notes || "";
    option.dataset.docsUrl = preset.docs_url || "";
    option.dataset.arch = preset.arch || "";
    option.dataset.release = preset.release || "";
    option.dataset.distro = preset.distro || "";
    select.appendChild(option);
  }

  if (previous && Array.from(select.options).some((o) => o.value === previous)) {
    select.value = previous;
  }
  applyOsPreset();
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function updatePresetSelectState(localAvailable) {
  form.os_preset.classList.toggle("cached-selected", !!localAvailable);
  form.os_preset.classList.toggle("download-needed", !localAvailable);
}

function applyOsPreset() {
  const selected = form.os_preset.selectedOptions[0];
  const profile = form.profile.value;
  const arch = profileArch(profile);
  if (!selected || !selected.value) {
    form.os_preset.classList.remove("cached-selected", "download-needed");
    osPresetNoteEl.textContent = `No preset selected for ${profile} (${arch}). You can provide Image URL or local file manually.`;
    catalogMetaEl.textContent = "Tip: choose a distro family/release or search to narrow options.";
    updateOsScopedOptionsVisibility();
    return;
  }

  const imageUrl = selected.dataset.imageUrl || "";
  const cachePath = selected.dataset.cachePath || "";
  const localAvailable = selected.dataset.localAvailable === "1";
  const imageSha = selected.dataset.imageSha256 || "";
  const notes = selected.dataset.notes || "";
  const docsUrl = selected.dataset.docsUrl || "";
  const distro = selected.dataset.distro || "";
  const release = selected.dataset.release || "";
  updatePresetSelectState(localAvailable);
  if (localAvailable && cachePath) {
    form.image_file.value = cachePath;
    form.image_url.value = "";
    osPresetNoteEl.textContent = `${distro} ${release} selected for ${profile} (${arch}). Verified cached image found locally 🎉`; 
  } else if (imageUrl) {
    form.image_url.value = imageUrl;
    form.image_file.value = "";
    osPresetNoteEl.textContent = `${distro} ${release} selected for ${profile} (${arch}). Image URL was populated 🌐`;
  } else if (profile === "pi3bplus" || profile === "pi4") {
    osPresetNoteEl.textContent = `${distro} ${release} selected for ${profile === "pi4" ? "Pi 4" : "Pi 3B+"}. The default Ubuntu 24.04 ARM64 image works on this board 🌐`;
  } else {
    osPresetNoteEl.textContent = `${distro} ${release} selected. Provide a direct Image URL or local Image File 🙂`;
  }

  if (imageSha && !form.image_sha256.value.trim()) {
    form.image_sha256.value = imageSha;
  }

  const statusPill = localAvailable
    ? `<span class="catalog-pill available">✅ Cached locally</span>`
    : `<span class="catalog-pill missing">⬇️ Needs download</span>`;
  const notesHtml = notes ? `<div>${escapeHtml(notes)}</div>` : "";
  if (docsUrl) {
    catalogMetaEl.innerHTML = `<div class="catalog-pills">${statusPill}</div>${notesHtml}<a href="${docsUrl}" target="_blank" rel="noopener noreferrer">Open distro download docs 📚</a>`;
  } else {
    const fallback = notes || "No docs URL for this preset. Provide image details manually.";
    catalogMetaEl.innerHTML = `<div class="catalog-pills">${statusPill}</div><div>${escapeHtml(fallback)}</div>`;
  }
  updateOsScopedOptionsVisibility();
}

function currentDistroFamilyForOptionScopes() {
  const selected = form.os_preset?.selectedOptions?.[0];
  const fromPreset = String(selected?.dataset?.distro || "").trim().toLowerCase();
  const family = fromPreset || String(form.distro_family?.value || "").trim().toLowerCase();
  return family === "raspberry pi os" ? "debian" : family;
}

function optionScopeMatches(el) {
  if (!cloudInitProfileScopeMatches(el)) return false;
  const distroFamily = currentDistroFamilyForOptionScopes();
  const allowedOsRaw = String(el.dataset.optionOs || "").trim().toLowerCase();
  if (allowedOsRaw) {
    const allowed = allowedOsRaw.split(",").map((v) => v.trim()).filter(Boolean);
    if (!allowed.some((v) => distroFamily.includes(v))) {
      return false;
    }
  }
  const requiredDistribution = String(el.dataset.optionDistribution || "").trim().toLowerCase();
  if (requiredDistribution) {
    const currentDistribution = String(form.distribution?.value || "").trim().toLowerCase();
    if (currentDistribution !== requiredDistribution) {
      return false;
    }
  }
  const allowedProfilesRaw = String(el.dataset.optionProfile || "").trim().toLowerCase();
  if (allowedProfilesRaw) {
    const allowedProfiles = allowedProfilesRaw.split(/\s+/).filter(Boolean);
    if (!allowedProfiles.includes(String(form.profile?.value || "").trim().toLowerCase())) {
      return false;
    }
  }
  const requiredRoles = String(el.dataset.optionRole || "").trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (requiredRoles.length) {
    const currentRole = String(document.getElementById("opt_k3s_role")?.value || "").trim().toLowerCase();
    if (!requiredRoles.includes(currentRole)) {
      return false;
    }
  }
  return true;
}
