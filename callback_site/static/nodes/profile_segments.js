function renderProfileSegmentOptions() {
  profileSegmentProfileEl.innerHTML = "<option value=''>Select profile...</option>";
  for (const profile of profiles) {
    const option = document.createElement("option");
    option.value = profile.id;
    option.textContent = profile.label;
    profileSegmentProfileEl.appendChild(option);
  }
}

function renderProfileSegments(segments) {
  profileSegmentListEl.innerHTML = "";
  const entries = Object.entries(segments || {}).sort(([a], [b]) => a.localeCompare(b));
  if (!entries.length) {
    profileSegmentListEl.innerHTML = '<div class="note">No saved profile segments yet.</div>';
    return;
  }

  for (const [profileId, items] of entries) {
    const card = document.createElement("div");
    card.className = "segment-card";
    const title = document.createElement("h4");
    title.textContent = `${profileId} (${Array.isArray(items) ? items.length : 0} segment${Array.isArray(items) && items.length === 1 ? "" : "s"})`;
    card.appendChild(title);
    if (Array.isArray(items)) {
      for (const item of items) {
        const meta = document.createElement("div");
        meta.className = "segment-meta";
        meta.textContent = `${item.name || "segment"} • updated ${formatTimestamp(item.updated_at || "")}`;
        card.appendChild(meta);
        const defaults = document.createElement("div");
        defaults.className = "segment-pill";
        defaults.textContent = item.defaults_json ? "defaults JSON" : "no defaults";
        const galaxy = document.createElement("div");
        galaxy.className = "segment-pill";
        galaxy.textContent = Array.isArray(item.galaxy_requirements) && item.galaxy_requirements.length ? `${item.galaxy_requirements.length} galaxy req` : "no galaxy req";
        card.appendChild(defaults);
        card.appendChild(galaxy);
        if (item.config_block) {
          const block = document.createElement("pre");
          block.className = "segment-block";
          block.textContent = item.config_block;
          card.appendChild(block);
        }
      }
    }
    profileSegmentListEl.appendChild(card);
  }
}

async function refreshProfileSegments() {
  try {
    const res = await fetch("/api/nodes/profile-segments");
    const data = await res.json();
    if (!res.ok || !data.ok) {
      throw new Error(data.error || "Failed to load profile segments");
    }
    renderProfileSegments(data.segments || {});
    profileSegmentStatusEl.textContent = `Loaded ${Object.keys(data.segments || {}).length} profile segment group${Object.keys(data.segments || {}).length === 1 ? "" : "s"}.`;
  } catch (err) {
    profileSegmentStatusEl.textContent = `Profile segment load error: ${String(err.message || err)}`;
  }
}

async function saveProfileSegment() {
  const profileId = profileSegmentProfileEl.value.trim();
  const name = profileSegmentNameEl.value.trim();
  if (!profileId || !name) {
    profileSegmentStatusEl.textContent = "Select a profile and give the segment a name first.";
    return;
  }
  const requirements = profileSegmentGalaxyEl.value
    .split(/\n|,/)
    .map((value) => value.trim())
    .filter(Boolean);
  const payload = {
    profile_id: profileId,
    segment_name: name,
    config_block: profileSegmentConfigEl.value.trim(),
    defaults_json: profileSegmentDefaultsEl.value.trim(),
    galaxy_requirements: requirements,
  };
  try {
    const res = await fetch("/api/nodes/profile-segments", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      throw new Error(data.error || "Failed to save profile segment");
    }
    profileSegmentStatusEl.textContent = `Saved profile segment '${name}' for ${profileId}.`;
    profileSegmentNameEl.value = "";
    profileSegmentConfigEl.value = "";
    profileSegmentDefaultsEl.value = "";
    profileSegmentGalaxyEl.value = "";
    await refreshProfileSegments();
  } catch (err) {
    profileSegmentStatusEl.textContent = `Profile segment save error: ${String(err.message || err)}`;
  }
}
