function nextLetterToken(token) {
  if (!/^[a-zA-Z]+$/.test(token)) {
    return null;
  }
  const isLower = token === token.toLowerCase();
  const isUpper = token === token.toUpperCase();
  if (!isLower && !isUpper) {
    return null;
  }
  const chars = token.toLowerCase().split("");
  let idx = chars.length - 1;
  while (idx >= 0 && chars[idx] === "z") {
    chars[idx] = "a";
    idx -= 1;
  }
  if (idx < 0) {
    chars.unshift("a");
  } else {
    chars[idx] = String.fromCharCode(chars[idx].charCodeAt(0) + 1);
  }
  const next = chars.join("");
  return isUpper ? next.toUpperCase() : next;
}

function nextNatoToken(token) {
  const lowered = String(token || "").toLowerCase();
  const index = NATO_SEQUENCE.indexOf(lowered);
  if (index < 0 || index >= NATO_SEQUENCE.length - 1) {
    return null;
  }
  const next = NATO_SEQUENCE[index + 1];
  if (token === token.toUpperCase()) {
    return next.toUpperCase();
  }
  if (/^[A-Z][a-z]+$/.test(token)) {
    return next.charAt(0).toUpperCase() + next.slice(1);
  }
  return next;
}

function nextNumericToken(token) {
  if (!/^\d+$/.test(token)) {
    return null;
  }
  const width = token.length;
  const nextNum = Number.parseInt(token, 10) + 1;
  const nextRaw = String(nextNum);
  if (width > 1 && token.startsWith("0")) {
    return nextRaw.padStart(width, "0");
  }
  return nextRaw;
}

function nextTailToken(token) {
  return nextNatoToken(token) || nextNumericToken(token) || nextLetterToken(token);
}

function computeNextHostname(hostname) {
  const current = String(hostname || "").trim();
  if (!current) {
    return "";
  }
  const tailMatch = current.match(/^(.*?)([A-Za-z]+|\d+)$/);
  if (!tailMatch) {
    return "";
  }
  const prefix = tailMatch[1] || "";
  const token = tailMatch[2] || "";
  const nextToken = nextTailToken(token);
  if (!nextToken) {
    return "";
  }
  return `${prefix}${nextToken}`;
}

function maybeIncrementHostnameAfterSuccess(job) {
  const finished = job && String(job.status || "") !== "queued" && String(job.status || "") !== "running";
  if (!finished || Number(job.exit_code) !== 0) {
    return;
  }
  const jobId = String(job.id || "").trim();
  if (!jobId) {
    return;
  }
  const priorJobId = String(localStorage.getItem(SD_HOSTNAME_INCREMENTED_JOB_KEY) || "").trim();
  if (priorJobId === jobId) {
    return;
  }
  localStorage.setItem(SD_HOSTNAME_INCREMENTED_JOB_KEY, jobId);

  const before = String(form.hostname?.value || "").trim();
  const after = computeNextHostname(before);
  if (!after || after === before) {
    return;
  }
  form.hostname.value = after;
  const note = document.createElement("div");
  note.className = "job-substatus";
  note.textContent = `Next hostname prepared: ${before} -> ${after}`;
  statusEl.appendChild(note);
}
