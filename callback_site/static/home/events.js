function eventReceivedTs(event) {
  const raw = event && event.received_at ? String(event.received_at) : "";
  if (!raw) return null;
  const normalized = raw.replace(" UTC", "Z").replace(" ", "T");
  const ts = Date.parse(normalized);
  return Number.isNaN(ts) ? null : ts;
}

function formatEventTimestamp(value) {
  const raw = String(value || "").trim();
  if (!raw) return "-";
  const normalized = raw.replace(" UTC", "Z").replace(" ", "T");
  const dt = new Date(normalized);
  if (Number.isNaN(dt.getTime())) {
    return raw;
  }
  return dt.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
    timeZoneName: "short",
  });
}

function updateEventFilterOptions(events) {
  const currentProfile = eventProfileEl.value;
  const currentStage = eventStageEl.value;
  const profiles = [...new Set(events.map((e) => e.profile || "unknown"))].sort();
  const stages = [...new Set(events.map((e) => e.stage || "phone_home"))].sort();

  eventProfileEl.innerHTML = "<option value=''>All profiles</option>";
  for (const profile of profiles) {
    const option = document.createElement("option");
    option.value = profile;
    option.textContent = profile;
    eventProfileEl.appendChild(option);
  }

  eventStageEl.innerHTML = "<option value=''>All stages</option>";
  for (const stage of stages) {
    const option = document.createElement("option");
    option.value = stage;
    option.textContent = stage;
    eventStageEl.appendChild(option);
  }

  if (currentProfile && profiles.includes(currentProfile)) {
    eventProfileEl.value = currentProfile;
  }
  if (currentStage && stages.includes(currentStage)) {
    eventStageEl.value = currentStage;
  }
}

function filterEvents(events) {
  const query = (eventSearchEl.value || "").trim().toLowerCase();
  const profile = eventProfileEl.value;
  const stage = eventStageEl.value;

  return events.filter((e) => {
    const evProfile = e.profile || "unknown";
    const evStage = e.stage || "phone_home";
    if (profile && evProfile !== profile) return false;
    if (stage && evStage !== stage) return false;
    if (!query) return true;
    const haystack = [
      e.hostname,
      e.fqdn,
      e.instance_id,
      e.source_ip,
      e.cert_subject,
      e.cert_fingerprint,
    ].map((v) => String(v || "").toLowerCase()).join(" ");
    return haystack.includes(query);
  });
}

function certSummary(event) {
  const subject = String(event.cert_subject || "").trim();
  const fingerprint = String(event.cert_fingerprint || "").trim();
  if (!subject && !fingerprint) {
    return "-";
  }
  if (subject && fingerprint) {
    return `${subject} (${fingerprint})`;
  }
  return subject || fingerprint;
}

function payloadSummary(event) {
  const raw = (event && typeof event.raw === "object" && event.raw) ? event.raw : {};
  const keys = Object.keys(raw);
  const stage = raw.stage || event.stage || "-";
  const normalizedProfile = event.profile || "unknown";
  const payloadProfile = event.raw_profile || "-";
  const profileSource = event.profile_source || "unknown";
  const envelope = {
    received_at: event.received_at || "",
    source_ip: event.source_ip || "",
    source_ip_source: event.source_ip_source || "",
    forwarded_for: event.forwarded_for || "",
    remote_addr: event.remote_addr || "",
    user_agent: event.user_agent || "",
    hostname: event.hostname || "",
    fqdn: event.fqdn || "",
    instance_id: event.instance_id || "",
    profile: normalizedProfile,
    raw_profile: payloadProfile,
    profile_source: profileSource,
    stage: event.stage || "",
    cert_subject: event.cert_subject || "",
    cert_issuer: event.cert_issuer || "",
    cert_fingerprint: event.cert_fingerprint || "",
    cert_present: Boolean(event.cert_present),
    cert_header_present: Boolean(event.cert_header_present),
    node_report_id: event.node_report_id || "",
    node_report_present: Boolean(event.node_report_present),
    raw,
  };
  return {
    line: `${keys.length} key${keys.length === 1 ? "" : "s"} | profile: ${normalizedProfile} (${profileSource}) | stage: ${stage} | source: ${event.source_ip || event.remote_addr || "-"}`,
    json: JSON.stringify(envelope, null, 2),
  };
}

function updateEventStats(all, visible) {
  const now = Date.now();
  const oneDayMs = 24 * 60 * 60 * 1000;
  const recentCount = all.filter((e) => {
    const ts = eventReceivedTs(e);
    return ts !== null && (now - ts) <= oneDayMs;
  }).length;
  const uniqueHosts = new Set(all.map((e) => e.hostname || e.fqdn || "-").filter((v) => v && v !== "-")).size;
  statVisibleEl.textContent = String(visible.length);
  statTotalEl.textContent = String(all.length);
  stat24hEl.textContent = String(recentCount);
  statHostsEl.textContent = String(uniqueHosts);

  const first = visible.length ? visible[0] : null;
  const latestAt = first && first.received_at ? formatEventTimestamp(first.received_at) : "n/a";
  eventMetaEl.textContent = `Showing ${visible.length} of ${all.length} events. Latest callback: ${latestAt}. Auto-refresh every 5s.`;
}

const payloadInspectPanel = document.getElementById("payload-inspect-panel");

const payloadInspectMeta = document.getElementById("payload-inspect-meta");

const payloadInspectJson = document.getElementById("payload-inspect-json");

let inspectedEventId = null;

let inspectedSnapshot = null;

const EVENTS_OLDER_KEY = "callback-older-expanded";

let olderExpanded = localStorage.getItem(EVENTS_OLDER_KEY) === "1";

const expandedEventIds = new Set();

const eventsToggleOlderBtn = document.getElementById("events-toggle-older");

function eventHostKey(event) {
  return String(event.hostname || event.fqdn || event.instance_id || "").trim().toLowerCase();
}

function updateOlderToggle(olderCount) {
  if (!eventsToggleOlderBtn) return;
  eventsToggleOlderBtn.disabled = olderCount === 0;
  eventsToggleOlderBtn.textContent = olderExpanded
    ? `Condense older (${olderCount})`
    : `Expand older (${olderCount})`;
}

function toggleOlderEvents() {
  olderExpanded = !olderExpanded;
  localStorage.setItem(EVENTS_OLDER_KEY, olderExpanded ? "1" : "0");
  expandedEventIds.clear();
  renderEvents(filterEvents(allEvents));
}

function eventIdentity(event) {
  return [
    String(event.received_at || ""),
    String(event.hostname || ""),
    String(event.instance_id || ""),
    String(event.stage || ""),
  ].join("|");
}

function showPayloadInspect(event) {
  const summary = payloadSummary(event);
  const id = eventIdentity(event);
  inspectedEventId = id;
  inspectedSnapshot = {
    id,
    event,
    summary,
  };
  payloadInspectMeta.textContent = summary.line;
  payloadInspectJson.textContent = summary.json;

  eventsBodyEl.querySelectorAll("tr.event-row-selected").forEach((r) => r.classList.remove("event-row-selected"));
  const allRows = eventsBodyEl.querySelectorAll("tr");
  for (const row of allRows) {
    if (row.dataset.eventId === id) {
      row.classList.add("event-row-selected");
      break;
    }
  }
}

function renderEvents(events) {
  eventsBodyEl.innerHTML = "";
  if (!events || !events.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 8;
    td.textContent = "No events received yet.";
    tr.appendChild(td);
    eventsBodyEl.appendChild(tr);
    updateOlderToggle(0);
    return;
  }

  // Events arrive newest first: the first one per host stays full, the rest are older.
  const seenHosts = new Set();
  let olderCount = 0;
  for (const e of events.slice(0, 200)) {
    const hostKey = eventHostKey(e);
    const isOlder = Boolean(hostKey) && seenHosts.has(hostKey);
    if (hostKey) seenHosts.add(hostKey);
    if (isOlder) olderCount += 1;
    const tr = document.createElement("tr");
    tr.className = "event-row-new";
    const eventId = eventIdentity(e);
    tr.dataset.eventId = eventId;
    const condensed = isOlder && !olderExpanded && !expandedEventIds.has(eventId);
    if (condensed) tr.classList.add("event-row-condensed");
    if (isOlder) tr.dataset.older = "1";
    if (inspectedEventId && inspectedEventId === eventId) {
      tr.classList.add("event-row-selected");
    }
    tr.style.cursor = "pointer";
    tr.title = "Click to inspect payload";
    const sourceIp = e.source_ip || e.remote_addr || "-";
    const sourceIpNote = e.source_ip_source
      ? `via ${e.source_ip_source}${e.forwarded_for ? ` (${e.forwarded_for})` : ""}`
      : (e.remote_addr ? `via remote_addr (${e.remote_addr})` : "");
    const values = [
      formatEventTimestamp(e.received_at),
      e.hostname || "-",
      e.instance_id || "-",
      e.fqdn || "-",
      e.profile || "unknown",
      e.stage || "phone_home",
      sourceIp,
      certSummary(e),
    ];

    values.forEach((v, idx) => {
      const td = document.createElement("td");
      if (idx === 0 && isOlder) {
        const twisty = document.createElement("span");
        twisty.className = "event-twisty";
        twisty.textContent = condensed ? "▸ " : "▾ ";
        twisty.title = condensed ? "Expand this older callback" : "Condense this older callback";
        twisty.addEventListener("click", (event) => {
          event.stopPropagation();
          if (expandedEventIds.has(eventId)) expandedEventIds.delete(eventId);
          else expandedEventIds.add(eventId);
          renderEvents(filterEvents(allEvents));
        });
        td.appendChild(twisty);
        td.appendChild(document.createTextNode(v));
        tr.appendChild(td);
        return;
      }
      if (idx === 4) {
        const span = document.createElement("span");
        span.className = "tag";
        span.textContent = v;
        td.appendChild(span);
      } else if (idx === 6 && sourceIpNote) {
        td.textContent = v;
        const note = document.createElement("div");
        note.className = "payload-kv";
        note.textContent = sourceIpNote;
        td.appendChild(note);
      } else {
        td.textContent = v;
      }
      tr.appendChild(td);
    });

    const stageTd = tr.children[5];
    if (stageTd) {
      const stageValue = values[5];
      stageTd.innerHTML = "";
      const stage = document.createElement("span");
      stage.className = `stage-pill stage-${String(stageValue).toLowerCase().replace(/[^a-z0-9_]+/g, "_")}`;
      stage.textContent = stageValue;
      stageTd.appendChild(stage);
    }

    tr.addEventListener("click", () => showPayloadInspect(e));
    eventsBodyEl.appendChild(tr);
  }
  updateOlderToggle(olderCount);

  if (inspectedEventId && inspectedSnapshot) {
    const match = events.find((e) => eventIdentity(e) === inspectedEventId);
    if (match) {
      const summary = payloadSummary(match);
      payloadInspectMeta.textContent = summary.line;
      payloadInspectJson.textContent = summary.json;
      inspectedSnapshot = { id: inspectedEventId, event: match, summary };
    } else {
      payloadInspectMeta.textContent = `${inspectedSnapshot.summary.line} (snapshot retained; event not in current filtered view)`;
      payloadInspectJson.textContent = inspectedSnapshot.summary.json;
    }
  }
}

async function loadEvents() {
  try {
    const res = await fetch("/api/cloud-init/events");
    const data = await res.json();
    if (!res.ok) {
      return;
    }
    allEvents = Array.isArray(data.events) ? data.events : [];
    updateCallbackActivity(allEvents);
    updateEventFilterOptions(allEvents);
    refreshBootLogHosts(allEvents);
    const visibleEvents = filterEvents(allEvents);
    updateEventStats(allEvents, visibleEvents);
    renderEvents(visibleEvents);
  } catch (_) {
    // Keep prior event table data if refresh fails.
  }
}
