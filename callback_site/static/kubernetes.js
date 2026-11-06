const body = document.body;
const toggleBtn = document.getElementById("sidebar-toggle");
const nodesTotalEl = document.getElementById("kube-nodes-total");
const nodesReadyEl = document.getElementById("kube-nodes-ready");
const controlPlaneEl = document.getElementById("kube-control-plane");
const podsEl = document.getElementById("kube-pods");
const namespacesEl = document.getElementById("kube-namespaces");
const jobsEl = document.getElementById("kube-jobs");
const nodesTotalHeroEl = document.getElementById("kube-nodes-total-hero");
const nodesReadyHeroEl = document.getElementById("kube-nodes-ready-hero");
const controlPlaneHeroEl = document.getElementById("kube-control-plane-hero");
const podsHeroEl = document.getElementById("kube-pods-hero");
const namespacesHeroEl = document.getElementById("kube-namespaces-hero");
const jobsHeroEl = document.getElementById("kube-jobs-hero");
const longhornHeroEl = document.getElementById("kube-longhorn-hero");
const runtimeServiceEl = document.getElementById("kube-runtime-service");
const runtimeServiceDetailEl = document.getElementById("kube-runtime-service-detail");
const kubeconfigEl = document.getElementById("kube-has-kubeconfig");
const binaryEl = document.getElementById("kube-binary");
const reasonEl = document.getElementById("kube-reason");
const lastErrorMiniEl = document.getElementById("kube-last-error-mini");
const observedAtEl = document.getElementById("kube-observed-at");
const readyPercentEl = document.getElementById("kube-ready-percent");
const runningPercentEl = document.getElementById("kube-running-percent");
const lastErrorEl = document.getElementById("kube-last-error");
const longhornEl = document.getElementById("kube-longhorn");
const longhornReadyEl = document.getElementById("kube-longhorn-ready");
const nodeRowsEl = document.getElementById("kube-node-rows");
const podRowsEl = document.getElementById("kube-pod-rows");
const namespaceRowsEl = document.getElementById("kube-namespace-rows");
const jobRowsEl = document.getElementById("kube-job-rows");
const factsListEl = document.getElementById("kube-facts-list");
const rawJsonEl = document.getElementById("kube-raw-json");
const statusEl = document.getElementById("kube-status");

function initSidebar() {
  const saved = localStorage.getItem("sidebar-collapsed");
  if (saved === "1") body.classList.add("sidebar-collapsed");
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function renderPill(value, kind) {
  const className = kind ? `kube-pill ${kind}` : "kube-pill";
  return `<span class="${className}">${escapeHtml(value)}</span>`;
}

function renderRows(items, columns) {
  if (!Array.isArray(items) || !items.length) {
    return `<tr><td colspan="${columns}">No rows returned.</td></tr>`;
  }
  return items.map((item) => item).join("");
}

function renderNodeRows(rows) {
  if (!Array.isArray(rows) || !rows.length) {
    return `<tr><td colspan="10">No nodes returned.</td></tr>`;
  }
  return rows.map((row) => {
    const ready = row.ready ? renderPill("Ready", "ok") : renderPill("Not Ready", "warn");
    const down = !row.ready && Number.isFinite(row.not_ready_minutes) ? ` <span class="kube-subtle">${escapeHtml(row.not_ready_minutes)}m</span>` : "";
    const action = row.ready
      ? ""
      : `<button type="button" class="kube-btn" data-remove-node="${escapeHtml(row.name || "")}" data-node-role="${escapeHtml(row.role || "")}">Remove</button>`;
    return `<tr>
      <td>${escapeHtml(row.name || "")}</td>
      <td>${ready}${down}</td>
      <td>${escapeHtml(row.role || "")}</td>
      <td>${escapeHtml(row.internal_ip || "")}</td>
      <td>${escapeHtml(row.external_ip || "")}</td>
      <td>${escapeHtml(row.labels || "")}</td>
      <td>${escapeHtml(row.os_image || "")}</td>
      <td>${escapeHtml(row.kernel || "")}</td>
      <td>${escapeHtml(row.container_runtime || "")}</td>
      <td>${action}</td>
    </tr>`;
  }).join("");
}

function renderPodRows(rows) {
  if (!Array.isArray(rows) || !rows.length) {
    return `<tr><td colspan="7">No pods returned.</td></tr>`;
  }
  return rows.map((row) => {
    const phaseKind = row.phase === "Running" ? "ok" : "warn";
    const created = row.created_at ? String(row.created_at).replace("T", " ").replace("Z", " UTC") : "-";
    return `<tr>
      <td>${escapeHtml(row.namespace || "")}</td>
      <td>${escapeHtml(row.name || "")}</td>
      <td>${renderPill(row.phase || "", phaseKind)}</td>
      <td>${escapeHtml(row.node || "")}</td>
      <td>${escapeHtml(row.ready_containers || 0)}</td>
      <td>${escapeHtml(row.restart_count || 0)}</td>
      <td>${escapeHtml(created)}</td>
    </tr>`;
  }).join("");
}

function renderNamespaceRows(rows) {
  if (!Array.isArray(rows) || !rows.length) {
    return `<tr><td colspan="4">No namespaces returned.</td></tr>`;
  }
  return rows.map((row) => {
    const phaseKind = row.phase === "Active" ? "ok" : "warn";
    return `<tr>
      <td>${escapeHtml(row.name || "")}</td>
      <td>${renderPill(row.phase || "", phaseKind)}</td>
      <td>${escapeHtml(row.running_pods || 0)}</td>
      <td>${escapeHtml(row.total_pods || 0)}</td>
    </tr>`;
  }).join("");
}

function renderJobRows(rows) {
  if (!Array.isArray(rows) || !rows.length) {
    return `<tr><td colspan="6">No jobs returned.</td></tr>`;
  }
  return rows.map((row) => {
    const status = String(row.status || "Unknown");
    const kind = status === "Running" ? "ok" : status === "Failed" ? "warn" : "";
    return `<tr>
      <td>${escapeHtml(row.namespace || "default")}</td>
      <td>${escapeHtml(row.name || "")}</td>
      <td>${renderPill(status, kind)}</td>
      <td>${escapeHtml(row.active || 0)}</td>
      <td>${escapeHtml(row.succeeded || 0)}</td>
      <td>${escapeHtml(row.failed || 0)}</td>
    </tr>`;
  }).join("");
}

toggleBtn.addEventListener("click", () => {
  body.classList.toggle("sidebar-collapsed");
  localStorage.setItem("sidebar-collapsed", body.classList.contains("sidebar-collapsed") ? "1" : "0");
});

async function loadKubernetesSummary() {
  statusEl.textContent = "Refreshing Kubernetes status...";
  try {
    const res = await fetch("/api/kubernetes/summary", { cache: "no-store" });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      statusEl.textContent = data.error || "Failed to load Kubernetes summary.";
      return;
    }

    nodesTotalEl.textContent = String(data.nodes_total || 0);
    nodesReadyEl.textContent = String(data.nodes_ready || 0);
    controlPlaneEl.textContent = String(data.control_plane_nodes || 0);
    podsEl.textContent = `${Number(data.pods_running || 0)} / ${Number(data.pods_total || 0)}`;
    namespacesEl.textContent = String(data.namespaces_total || 0);
    jobsEl.textContent = `${Number(data.jobs_running || 0)} / ${Number(data.jobs_total || 0)}`;
    longhornEl.textContent = `${Number(data.longhorn_pods_running || 0)} / ${Number(data.longhorn_pods_total || 0)}`;
    nodesTotalHeroEl.textContent = String(data.nodes_total || 0);
    nodesReadyHeroEl.textContent = String(data.nodes_ready || 0);
    controlPlaneHeroEl.textContent = String(data.control_plane_nodes || 0);
    podsHeroEl.textContent = `${Number(data.pods_running || 0)} / ${Number(data.pods_total || 0)}`;
    namespacesHeroEl.textContent = String(data.namespaces_total || 0);
    jobsHeroEl.textContent = `${Number(data.jobs_running || 0)} / ${Number(data.jobs_total || 0)}`;
    longhornHeroEl.textContent = data.longhorn_ready ? "ready" : (data.longhorn_namespace_present ? "degraded" : "absent");

    readyPercentEl.textContent = `${data.nodes_total ? Math.round((Number(data.nodes_ready || 0) / Number(data.nodes_total || 1)) * 100) : 0}%`;
    runningPercentEl.textContent = `${data.pods_total ? Math.round((Number(data.pods_running || 0) / Number(data.pods_total || 1)) * 100) : 0}%`;
    lastErrorEl.textContent = data.last_error ? String(data.last_error) : "none";
    observedAtEl.textContent = data.observed_at ? String(data.observed_at) : "--";
    longhornReadyEl.textContent = data.longhorn_ready ? "yes" : "no";

    const runtime = data.runtime && typeof data.runtime === "object" ? data.runtime : {};
    const activeService = String(runtime.active_service || "none");
    const reason = String(runtime.reason || "");
    const hasKubeconfig = runtime.has_kubeconfig === true ? "yes" : "no";
    const lastErrorText = data.last_error ? String(data.last_error) : "none";
    runtimeServiceEl.textContent = activeService;
    runtimeServiceDetailEl.textContent = activeService;
    kubeconfigEl.textContent = hasKubeconfig;
    binaryEl.textContent = String(runtime.k3s_binary || "none");
    reasonEl.textContent = reason || "--";
    lastErrorMiniEl.textContent = lastErrorText;

    if (!data.available) {
      statusEl.textContent = `Kubernetes runtime is not currently active.\nReason: ${reason}`;
      nodeRowsEl.innerHTML = `<tr><td colspan="10">Runtime inactive.</td></tr>`;
      podRowsEl.innerHTML = `<tr><td colspan="7">Runtime inactive.</td></tr>`;
      namespaceRowsEl.innerHTML = `<tr><td colspan="4">Runtime inactive.</td></tr>`;
      jobRowsEl.innerHTML = `<tr><td colspan="6">Runtime inactive.</td></tr>`;
      factsListEl.innerHTML = [
        `Runtime service: ${escapeHtml(activeService)}`,
        `Reason: ${escapeHtml(reason)}`,
        `Kubeconfig present: ${escapeHtml(hasKubeconfig)}`,
        `Binary: ${escapeHtml(String(runtime.k3s_binary || "none"))}`,
        `Last error: ${escapeHtml(lastErrorText)}`,
      ].map((line) => `<li>${line}</li>`).join("");
      rawJsonEl.textContent = JSON.stringify(data, null, 2);
      return;
    }

    let statusText = `Runtime service: ${activeService}\nKubeconfig present: ${hasKubeconfig}\nBinary: ${runtime.k3s_binary || "none"}`;
    if (data.last_error) {
      statusText += `\nWarning: ${data.last_error}`;
    }
    statusEl.textContent = statusText;
    nodeRowsEl.innerHTML = renderNodeRows(Array.isArray(data.node_rows) ? data.node_rows : []);
    podRowsEl.innerHTML = renderPodRows(Array.isArray(data.pod_rows) ? data.pod_rows : []);
    namespaceRowsEl.innerHTML = renderNamespaceRows(Array.isArray(data.namespace_rows) ? data.namespace_rows : []);
    jobRowsEl.innerHTML = renderJobRows(Array.isArray(data.job_rows) ? data.job_rows : []);
    factsListEl.innerHTML = [
      `Observed at ${escapeHtml(data.observed_at || "")}`,
      `Runtime service ${escapeHtml(activeService)}`,
      `Kubeconfig ${escapeHtml(hasKubeconfig)} · binary ${escapeHtml(String(runtime.k3s_binary || "none"))}`,
      `Nodes ready ${escapeHtml(data.nodes_ready || 0)} of ${escapeHtml(data.nodes_total || 0)}`,
      `Pods running ${escapeHtml(data.pods_running || 0)} of ${escapeHtml(data.pods_total || 0)}`,
      `Jobs running ${escapeHtml(data.jobs_running || 0)} of ${escapeHtml(data.jobs_total || 0)}`,
      `Longhorn ${escapeHtml(data.longhorn_pods_running || 0)} of ${escapeHtml(data.longhorn_pods_total || 0)} pods · ready ${escapeHtml(data.longhorn_ready ? "yes" : "no")}`,
      `Namespaces tracked ${escapeHtml(data.namespaces_total || 0)}`,
      `Last error ${escapeHtml(lastErrorText)}`,
    ].map((line) => `<li>${line}</li>`).join("");
    rawJsonEl.textContent = JSON.stringify(data, null, 2);
  } catch (err) {
    statusEl.textContent = `Kubernetes summary error: ${String(err && err.message ? err.message : err)}`;
    rawJsonEl.textContent = statusEl.textContent;
  }
}

const clearNodesBtn = document.getElementById("kube-clear-old-nodes");
const clearStatusEl = document.getElementById("kube-clear-status");
const CONTROL_PLANE_WARNING = "Removing a control-plane node also removes its etcd member.";

async function postClearNodes(dryRun) {
  const res = await fetch("/api/kubernetes/nodes/clear", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ dry_run: dryRun, min_minutes: 10 }),
  });
  return res.json();
}

async function clearOldNodes() {
  clearNodesBtn.disabled = true;
  clearStatusEl.textContent = "Checking for old nodes...";
  try {
    const preview = await postClearNodes(true);
    if (!preview.ok) {
      clearStatusEl.textContent = preview.error || "Could not list nodes.";
      return;
    }
    const targets = Array.isArray(preview.would_remove) ? preview.would_remove : [];
    if (!targets.length) {
      const waiting = Array.isArray(preview.waiting) ? preview.waiting.length : 0;
      clearStatusEl.textContent = waiting
        ? `Nothing to clear yet; ${waiting} NotReady node(s) are still inside the 10 minute grace period.`
        : "No old NotReady nodes to clear.";
      return;
    }
    const lines = targets.map((n) => `- ${n.name} (${n.role}, down ${n.not_ready_minutes}m)`).join("\n");
    const warn = targets.some((n) => n.role === "control-plane") ? `\n\n${CONTROL_PLANE_WARNING}` : "";
    if (!window.confirm(`Remove these NotReady nodes from the cluster?\n\n${lines}${warn}`)) {
      clearStatusEl.textContent = "Cancelled.";
      return;
    }
    const done = await postClearNodes(false);
    const removed = Array.isArray(done.removed) ? done.removed : [];
    const failed = Array.isArray(done.failed) ? done.failed : [];
    clearStatusEl.textContent = failed.length
      ? `Removed ${removed.length}; failed: ${failed.map((f) => `${f.name} (${f.error})`).join(", ")}`
      : `Removed ${removed.length} node(s): ${removed.join(", ") || "none"}`;
    await loadKubernetesSummary();
  } catch (err) {
    clearStatusEl.textContent = `Clear failed: ${String(err && err.message ? err.message : err)}`;
  } finally {
    clearNodesBtn.disabled = false;
  }
}

async function removeNode(button) {
  const name = button.dataset.removeNode || "";
  if (!name) return;
  const warn = button.dataset.nodeRole === "control-plane" ? `\n\n${CONTROL_PLANE_WARNING}` : "";
  if (!window.confirm(`Remove NotReady node ${name} from the cluster?${warn}`)) return;
  button.disabled = true;
  try {
    const res = await fetch(`/api/kubernetes/nodes/${encodeURIComponent(name)}`, { method: "DELETE" });
    const data = await res.json();
    clearStatusEl.textContent = res.ok && data.ok
      ? `Removed ${name}.`
      : `Could not remove ${name}: ${data.error || (data.failed && data.failed[0] && data.failed[0].error) || res.status}`;
    await loadKubernetesSummary();
  } catch (err) {
    clearStatusEl.textContent = `Remove failed: ${String(err && err.message ? err.message : err)}`;
    button.disabled = false;
  }
}

clearNodesBtn.addEventListener("click", clearOldNodes);
nodeRowsEl.addEventListener("click", (event) => {
  const button = event.target.closest("[data-remove-node]");
  if (button) removeNode(button);
});

initSidebar();
loadKubernetesSummary();
setInterval(loadKubernetesSummary, 20000);
window.addEventListener("focus", loadKubernetesSummary);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    loadKubernetesSummary();
  }
});
