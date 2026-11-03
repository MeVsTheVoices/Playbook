function renderJob(job) {
  if (job.status === "queued" || job.status === "running") {
    flashMascotEl.classList.add("active");
  } else {
    flashMascotEl.classList.remove("active");
  }
  const statusBadgeClass = job.status === "running" ? "running" : (job.exit_code === 0 ? "completed" : "failed");
  const statusEmoji = job.status === "running" ? "⏳" : (job.exit_code === 0 ? "✅" : "🔥");
  const statusNudge = job.status === "running"
    ? deriveRunningHint(job)
    : (job.exit_code === 0
      ? "Write complete. This SD card now has main-character energy."
      : "Something went sideways. Logs below have the tea.");
  const timingInfo = formatJobTiming(job);
  const heartbeat = job.status === "running" ? "Live check: active and polling every ~1s." : "Job polling complete.";
  statusEl.innerHTML = `
    <div class="job-status-header">
      <div>
        <span class="job-status-badge ${statusBadgeClass}">${statusEmoji} ${job.status.toUpperCase()}</span>
        <span style="color: #8b9ab2; font-size: 0.82rem; margin-left: 0.4rem;">ID: <code style="color: #a5f3fc; font-size: 0.78rem;">${job.id.substring(0, 12)}...</code></span>
      </div>
      ${timingInfo ? `<div class="job-timing">${timingInfo}</div>` : ""}
    </div>
    <div class="hint" style="margin-bottom: 0.45rem;">${statusNudge}</div>
    <div class="job-substatus">${heartbeat}</div>
    <div class="job-actions">
      <a href="/images" target="_self">📦 Image Library</a>
      <a href="/images#image-download-catalog" target="_self">⬇️ Download Catalog</a>
      <a href="/images#retrieved-image-manager" target="_self">💾 Managed Images</a>
    </div>
  `;
  const out = (job.stdout || "").trim();
  const err = (job.stderr || "").trim();
  const compactOutLines = compactLogLines(out ? out.split("\n") : []);
  const compactErrLines = compactLogLines(err ? err.split("\n") : []);
  const maxLines = 140;
  const shownOut = compactOutLines.length > maxLines
    ? ["... output trimmed ...", ...compactOutLines.slice(-maxLines)].join("\n")
    : (compactOutLines.join("\n") || "(none)");
  const shownErr = compactErrLines.length > maxLines
    ? ["... output trimmed ...", ...compactErrLines.slice(-maxLines)].join("\n")
    : (compactErrLines.join("\n") || "(none)");
  const body = [
    `exit_code: ${job.exit_code === null ? "running" : job.exit_code}`,
    "",
    "STDOUT:",
    shownOut,
    "",
    "STDERR:",
    shownErr,
  ].join("\n");
  logEl.innerHTML = highlightLogText(body);
  updateSdActivityFromJob(job);
  maybeIncrementHostnameAfterSuccess(job);
}

function formatJobTiming(job) {
  if (!job.created_at) return "";
  const createdMs = new Date(job.created_at).getTime();
  const finishedMs = job.finished_at ? new Date(job.finished_at).getTime() : Date.now();
  const durationSec = Math.round((finishedMs - createdMs) / 1000);
  const duration = durationSec > 60
    ? `${Math.floor(durationSec / 60)}m ${durationSec % 60}s`
    : `${durationSec}s`;
  return `Duration: ${duration}${job.status === "running" ? " (ongoing)" : ""}`;
}

function deriveRunningHint(job) {
  const out = String(job.stdout || "");
  if (/Decompressing image/i.test(out)) {
    return "Decompressing image bits. CPU is earning snacks.";
  }
  if (/Flashing image/i.test(out) || /copied,\s*\d+(?:\.\d+)?\s*[kMGT]?B\/s/i.test(out)) {
    return "Writing image to disk. Final sync can look quiet for a moment.";
  }
  if (/Downloading image/i.test(out) || /Download progress:/i.test(out)) {
    return "Downloading image payload. Internet and patience in tandem.";
  }
  return "Flashing in progress. Tiny bytes, big ambitions.";
}

function escapeHtml(text) {
  return String(text)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function highlightLogText(text) {
  const tokenRegex = /\b(progress|download|copying|copied|records|mounting|injecting|hash|verified|skipped|failed|error|done|uname|linux|kernel|cloud-init|ubuntu|flashing|decompressing|device|target|partition|formatted|written|starting|running|stopped|status)\b/gi;
  const pathRegex = /(\/[\w\/.\-]+|(?:[\w\-]+\.)+[\w\-]+)/g;
  const warnRegex = /\b(failed|error|aborted|invalid|missing|skipped)\b/gi;
  const okRegex = /\b(done|verified|completed|ready|ok|success)\b/gi;
  const infoRegex = /\b(starting|running|processing|preparing|mounted|unmounted|detected)\b/gi;
  const highlightRegex = /\b(bytes|copied|written|verified|checksum)\b/gi;
  const headingRegex = /^(exit_code:|STDOUT:|STDERR:)$/;

  const escaped = escapeHtml(text);
  const lines = escaped.split("\n");
  const counts = new Map();

  for (const line of lines) {
    const matches = line.matchAll(tokenRegex);
    for (const match of matches) {
      const key = (match[1] || "").toLowerCase();
      counts.set(key, (counts.get(key) || 0) + 1);
    }
  }

  return lines.map((line) => {
    if (headingRegex.test(line)) {
      return `<span class="kv">${line}</span>`;
    }
    let out = line;
    out = replaceOutsideTags(out, tokenRegex, (m) => {
      const key = m.toLowerCase();
      if ((counts.get(key) || 0) >= 2) {
        return `<span class="term">${m}</span>`;
      }
      return m;
    });
    out = replaceOutsideTags(out, warnRegex, (m) => `<span class="term-warn">${m}</span>`);
    out = replaceOutsideTags(out, okRegex, (m) => `<span class="term-ok">${m}</span>`);
    out = replaceOutsideTags(out, infoRegex, (m) => `<span class="term-info">${m}</span>`);
    out = replaceOutsideTags(out, highlightRegex, (m) => `<span class="term-highlight">${m}</span>`);
    out = replaceOutsideTags(out, pathRegex, (m) => {
      if (m.startsWith("/") || m.includes(".")) {
        return `<span class="term-path">${m}</span>`;
      }
      return m;
    });
    return out;
  }).join("\n");
}

function replaceOutsideTags(input, regex, replacer) {
  return input
    .split(/(<[^>]+>)/g)
    .map((segment) => (segment.startsWith("<") ? segment : segment.replace(regex, replacer)))
    .join("");
}

function compactLogLines(lines) {
  const flattened = lines
    .flatMap((line) => String(line).split("\r"))
    .flatMap((line) => String(line).split("\n"));

  const out = [];
  const streamIndex = {};
  const seen = new Set();
  let lastLine = "";
  let repeatCount = 0;

  function classifyStream(line) {
    if (/^[A-Za-z ]+progress:/i.test(line)) return "generic-progress";
    if (/^\d+ bytes .* copied, .* s, .*\/s$/.test(line)) return "dd-progress";
    return "";
  }

  function flushRepeat() {
    if (!lastLine) return;
    if (repeatCount > 1) {
      out.push(`${lastLine} (repeated ${repeatCount}x)`);
    } else if (!seen.has(lastLine)) {
      out.push(lastLine);
      seen.add(lastLine);
    }
    lastLine = "";
    repeatCount = 0;
  }

  for (const raw of flattened) {
    const line = raw.trimEnd();
    if (!line) {
      flushRepeat();
      if (out.length && out[out.length - 1] !== "") out.push("");
      continue;
    }

    const stream = classifyStream(line);
    if (stream) {
      flushRepeat();
      if (streamIndex[stream] !== undefined) {
        out[streamIndex[stream]] = line;
      } else {
        streamIndex[stream] = out.length;
        out.push(line);
      }
      continue;
    }

    if (line === lastLine) {
      repeatCount += 1;
    } else {
      flushRepeat();
      lastLine = line;
      repeatCount = 1;
    }
  }

  flushRepeat();
  return out;
}
