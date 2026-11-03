async function fetchLatestActiveSdJob() {
  try {
    const res = await fetch("/api/sd/jobs");
    const data = await res.json();
    if (!res.ok || !data.ok) {
      return null;
    }
    const jobs = Array.isArray(data.jobs) ? data.jobs : [];
    if (!jobs.length) {
      return null;
    }
    return jobs[0];
  } catch (_) {
    return null;
  }
}

async function pollJob() {
  if (!currentJob) return;
  const res = await fetch(`/api/sd/jobs/${currentJob}`);
  const data = await res.json();
  if (!data.ok) {
    statusEl.textContent = data.error || "Failed to load job status";
    updateSdActivityFromJob({ status: "failed", exit_code: 1, stdout: "", stderr: data.error || "poll failed" });
    return;
  }
  renderJob(data.job);
  if (data.job.status === "queued" || data.job.status === "running") {
    localStorage.setItem(SD_ACTIVE_JOB_KEY, currentJob);
    setTimeout(pollJob, 1200);
  } else {
    localStorage.removeItem(SD_ACTIVE_JOB_KEY);
  }
}

async function resumeActiveJobIfAny() {
  const savedJobId = String(localStorage.getItem(SD_ACTIVE_JOB_KEY) || "").trim();
  if (savedJobId) {
    currentJob = savedJobId;
    statusEl.textContent = `Resuming active job: ${currentJob}`;
    await pollJob();
    return;
  }

  const latestActiveJob = await fetchLatestActiveSdJob();
  if (!latestActiveJob || !latestActiveJob.id) {
    return;
  }
  currentJob = latestActiveJob.id;
  localStorage.setItem(SD_ACTIVE_JOB_KEY, currentJob);
  statusEl.textContent = `Resuming active job: ${currentJob}`;
  await pollJob();
}
