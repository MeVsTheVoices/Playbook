function initSidebar() {
  const saved = localStorage.getItem("sidebar-collapsed");
  if (saved === "1") {
    body.classList.add("sidebar-collapsed");
  }
}

function setActivityCard(cardEl, stateEl, detailEl, tone, stateText, detailText) {
  if (!cardEl || !stateEl || !detailEl) {
    return;
  }
  cardEl.classList.remove("waiting", "ok", "warn", "bad");
  cardEl.classList.add(tone);
  stateEl.textContent = stateText;
  detailEl.textContent = detailText;
}

function refreshActivityBanner() {
  if (!activityBannerEl) {
    return;
  }
  activityBannerEl.href = "#sd-form";
  if (activityMood.sd === "running") {
    activityBannerEl.className = "activity-banner waiting";
    activityBannerEl.textContent = "⏳ SD write in progress. Byte parade marching, please keep cable wiggles to a minimum. 🧃";
    return;
  }
  if (activityMood.sd === "failed") {
    activityBannerEl.href = "#job-log";
    activityBannerEl.className = "activity-banner bad";
    activityBannerEl.textContent = "🔥 SD flow needs attention. Check logs below for the exact wobble point.";
    return;
  }
  if (activityMood.callback === "active") {
    activityBannerEl.href = "#callback-dashboard";
    activityBannerEl.className = "activity-banner ok";
    activityBannerEl.textContent = "📡 Fresh callbacks are landing. Fleet heartbeat looks alive and chatty. 💚";
    return;
  }
  if (activityMood.device === "offline") {
    activityBannerEl.className = "activity-banner warn";
    activityBannerEl.textContent = deviceBackend === "helper"
      ? "🧰 Flash helper is starting or reconnecting. Discovery will retry while this page is visible."
      : "🔌 Device watcher is reconnecting. Snapshot polling will keep watch meanwhile.";
    return;
  }
  activityBannerEl.className = "activity-banner waiting";
  activityBannerEl.textContent = "🌼 Queue vibe: calm and ready. Kick off a task and this dock will narrate the waiting game.";
}

function updateSdActivityFromJob(job) {
  const status = String(job?.status || "").toLowerCase();
  if (status === "queued" || status === "running") {
    activityMood.sd = "running";
    setActivityCard(
      activitySdCardEl,
      activitySdStateEl,
      activitySdDetailEl,
      "waiting",
      "⏳ Cooking",
      deriveRunningHint(job)
    );
  } else if (job && Number(job.exit_code) === 0) {
    activityMood.sd = "done";
    setActivityCard(
      activitySdCardEl,
      activitySdStateEl,
      activitySdDetailEl,
      "ok",
      "✅ Complete",
      "Image write finished. This card is ready for victory lap duty. 🥳"
    );
  } else if (job) {
    activityMood.sd = "failed";
    setActivityCard(
      activitySdCardEl,
      activitySdStateEl,
      activitySdDetailEl,
      "bad",
      "🔥 Needs Fix",
      "Job ended with issues. Scroll logs for clues and rerun when ready."
    );
  } else {
    activityMood.sd = "idle";
    setActivityCard(
      activitySdCardEl,
      activitySdStateEl,
      activitySdDetailEl,
      "waiting",
      "😴 Idle",
      "No SD write is running right now."
    );
  }
  refreshActivityBanner();
}

function updateDeviceActivity(devicesCount, connected) {
  if (connected) {
    activityMood.device = "online";
    setActivityCard(
      activityDeviceCardEl,
      activityDeviceStateEl,
      activityDeviceDetailEl,
      "ok",
      deviceBackend === "helper" ? `🧰 Helper (${devicesCount})` : `🔌 Online (${devicesCount})`,
      devicesCount > 0
        ? deviceBackend === "helper"
          ? "Flash helper detected targets. Review device identity before writing."
          : "Removable target(s) detected and ready."
        : "Connected but no removable target detected yet."
    );
  } else {
    activityMood.device = "offline";
    setActivityCard(
      activityDeviceCardEl,
      activityDeviceStateEl,
      activityDeviceDetailEl,
      "warn",
      deviceBackend === "helper" ? "🧰 Warming up" : "🛰️ Reconnecting",
      deviceBackend === "helper"
        ? "Flash helper is starting or disconnected. Visible-page discovery retries."
        : "Live device stream dropped. Fallback polling remains active."
    );
  }
  refreshActivityBanner();
}

function formatAgeFromMs(ageMs) {
  if (!Number.isFinite(ageMs) || ageMs < 0) {
    return "just now";
  }
  const sec = Math.floor(ageMs / 1000);
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hour = Math.floor(min / 60);
  return `${hour}h ${min % 60}m ago`;
}

function updateCallbackActivity(events) {
  const list = Array.isArray(events) ? events : [];
  if (!list.length) {
    activityMood.callback = "quiet";
    setActivityCard(
      activityCallbackCardEl,
      activityCallbackStateEl,
      activityCallbackDetailEl,
      "waiting",
      "🫥 Quiet",
      "Waiting for nodes to check in."
    );
    refreshActivityBanner();
    return;
  }

  const latest = list[0] || {};
  const ts = eventReceivedTs(latest);
  const age = ts === null ? "time unknown" : formatAgeFromMs(Date.now() - ts);
  const host = String(latest.hostname || latest.fqdn || "unknown-node");
  const stage = String(latest.stage || "phone_home");
  activityMood.callback = "active";
  setActivityCard(
    activityCallbackCardEl,
    activityCallbackStateEl,
    activityCallbackDetailEl,
    "ok",
    `📡 Live (${age})`,
    `${host} reported stage '${stage}'.`
  );
  refreshActivityBanner();
}
