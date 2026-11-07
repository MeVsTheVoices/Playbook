const body = document.body;
const toggleBtn = document.getElementById("sidebar-toggle");
const ACL_DRAFT_STORAGE_KEY = "tailscale-acl-draft-v2";

function initSidebar() {
  const saved = localStorage.getItem("sidebar-collapsed");
  if (saved === "1") {
    body.classList.add("sidebar-collapsed");
  }
}

if (toggleBtn) {
  toggleBtn.addEventListener("click", () => {
    body.classList.toggle("sidebar-collapsed");
    localStorage.setItem("sidebar-collapsed", body.classList.contains("sidebar-collapsed") ? "1" : "0");
  });
}

const TEXT_FIELDS = ["rack_name", "tailnet", "admin_group", "admin_members", "homelab_tag", "traffic", "extra_ports", "subnets", "ssh_user"];
const CHECK_FIELDS = ["pi_servers", "subnet_access", "exit_node", "ssh", "ssh_root", "tests", "deny_tests", "toolkit_funnel"];
const POLICY_DEBOUNCE_MS = 250;

let policyRequestSeq = 0;
let policyTimer = null;
let defaultInputs = null;

function boardTagInputs() {
  return Array.from(document.querySelectorAll("[data-board-tag]"));
}

// Reads every input into one object; null when the inputs panel has not loaded yet.
function collectInputs() {
  const admin = document.getElementById("admin_group");
  if (!admin) {
    return null;
  }
  const values = {};
  TEXT_FIELDS.forEach((id) => {
    const el = document.getElementById(id);
    if (el) values[id] = el.value;
  });
  CHECK_FIELDS.forEach((id) => {
    const el = document.getElementById(id);
    if (el) values[id] = el.checked;
  });
  values.board_tags = {};
  boardTagInputs().forEach((el) => {
    values.board_tags[el.dataset.boardTag] = el.value;
  });
  return values;
}

function applyInputs(values) {
  if (!values || typeof values !== "object") {
    return false;
  }
  let applied = false;
  TEXT_FIELDS.forEach((id) => {
    const el = document.getElementById(id);
    if (el && values[id] !== undefined && values[id] !== null) {
      el.value = String(values[id]);
      applied = true;
    }
  });
  CHECK_FIELDS.forEach((id) => {
    const el = document.getElementById(id);
    if (el && typeof values[id] === "boolean") {
      el.checked = values[id];
      applied = true;
    }
  });
  const tags = values.board_tags && typeof values.board_tags === "object" ? values.board_tags : {};
  boardTagInputs().forEach((el) => {
    if (typeof tags[el.dataset.boardTag] === "string") {
      el.value = tags[el.dataset.boardTag];
      applied = true;
    }
  });
  return applied;
}

function readAclDraft() {
  try {
    const raw = localStorage.getItem(ACL_DRAFT_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch (_) {
    return null;
  }
}

function saveAclDraft(values) {
  try {
    localStorage.setItem(ACL_DRAFT_STORAGE_KEY, JSON.stringify(values));
  } catch (_) {
    // Ignore storage quota or privacy mode failures.
  }
}

function restoreAclDraft() {
  const status = document.getElementById("acl-draft-status");
  const draft = readAclDraft();
  if (applyInputs(draft)) {
    if (status) {
      status.textContent = "Restored your last-used Tailscale ACL configuration.";
    }
    return true;
  }
  if (status) {
    status.textContent = "Using the default ACL suggestions until you generate a new draft.";
  }
  return false;
}

function setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

function renderNotices(errors, warnings) {
  const list = document.getElementById("policy_warnings");
  if (!list) return;
  list.replaceChildren();
  const add = (level, text) => {
    const item = document.createElement("li");
    item.className = `notice notice-${level}`;
    item.textContent = text;
    list.appendChild(item);
  };
  (errors || []).forEach((text) => add("error", text));
  (warnings || []).forEach((entry) => add(entry.level === "info" ? "info" : "warn", entry.text));
}

function renderRules(rules) {
  const body = document.querySelector("#policy_rules tbody");
  if (!body) return;
  body.replaceChildren();
  (rules || []).forEach((rule) => {
    const row = document.createElement("tr");
    [rule.name, rule.src.join(", "), rule.dst.join(", "), rule.ports.join(", "), rule.why].forEach((text) => {
      const cell = document.createElement("td");
      cell.textContent = text;
      row.appendChild(cell);
    });
    body.appendChild(row);
  });
}

function syncOverview(inputs, summary) {
  setText("ov-rack", (inputs.rack_name || "").trim() || "Not set");
  setText("ov-tailnet", (inputs.tailnet || "").trim() || "Not set");
  setText("ov-tags", summary ? String(summary.tags.length) : "0");
  setText("ov-grants", summary ? String(summary.grants) : "0");
  setText("ov-route-count", summary ? String(summary.routes) : "0");
  setText("ov-tests", summary ? String(summary.tests) : "0");
  updateSubnetCounter(summary ? summary.routes : 0);
}

async function generatePolicy() {
  const inputs = collectInputs();
  const out = document.getElementById("policy_output");
  if (!inputs || !out) {
    return;
  }
  const status = document.getElementById("policy_status");
  const seq = ++policyRequestSeq;

  let data;
  try {
    const res = await fetch("/api/tailscale/policy", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        admin: inputs.admin_group,
        admin_members: inputs.admin_members,
        homelab_tag: inputs.homelab_tag,
        board_tags: inputs.board_tags,
        traffic: inputs.traffic,
        pi_servers: inputs.pi_servers,
        extra_ports: inputs.extra_ports,
        subnets: inputs.subnets,
        subnet_access: inputs.subnet_access,
        exit_node: inputs.exit_node,
        ssh: inputs.ssh,
        ssh_root: inputs.ssh_root,
        ssh_user: inputs.ssh_user,
        tests: inputs.tests,
        deny_tests: inputs.deny_tests,
        toolkit_funnel: inputs.toolkit_funnel,
      }),
    });
    data = await res.json();
  } catch (_) {
    if (seq === policyRequestSeq && status) status.textContent = "Could not reach the server to build the policy.";
    return;
  }
  if (seq !== policyRequestSeq) {
    return; // a newer edit superseded this response
  }

  saveAclDraft(inputs);
  if (!data.ok) {
    out.textContent = "Fix the inputs listed above to see the policy.";
    renderNotices(data.errors, []);
    renderRules([]);
    syncOverview(inputs, null);
    if (status) status.textContent = "The inputs have errors.";
  } else {
    out.textContent = data.policy_text;
    renderNotices([], data.warnings);
    renderRules(data.rules);
    syncOverview(inputs, data.summary);
    if (status) {
      const stamp = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
      status.textContent = `Updated ${stamp}: ${data.summary.grants} grants, ${data.summary.routes} routes, ${data.summary.tests} tests`;
    }
  }
  refreshFieldCards();
}

function schedulePolicy() {
  clearTimeout(policyTimer);
  policyTimer = setTimeout(generatePolicy, POLICY_DEBOUNCE_MS);
}

function updateSubnetCounter(count) {
  const counter = document.getElementById("subnet_counter");
  if (!counter) {
    return;
  }
  counter.textContent = `${count} route${count === 1 ? "" : "s"}`;
}

function refreshFieldCards() {
  const fieldCards = document.querySelectorAll(".field-card");
  fieldCards.forEach((card) => {
    const input = card.querySelector("input:not([type=checkbox]), textarea");
    if (!input) {
      return;
    }
    card.classList.toggle("field-ready", !!input.value.trim());
  });
}

async function copyPolicyOutput() {
  const out = document.getElementById("policy_output");
  const status = document.getElementById("policy_status");
  if (!out) {
    return;
  }
  const text = out.textContent || "";
  if (!text.trim()) {
    return;
  }
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const ghost = document.createElement("textarea");
      ghost.value = text;
      document.body.appendChild(ghost);
      ghost.select();
      document.execCommand("copy");
      document.body.removeChild(ghost);
    }
    if (status) {
      status.textContent = "Copied policy JSON to clipboard.";
    }
  } catch (error) {
    if (status) {
      status.textContent = "Copy failed. Select and copy manually.";
    }
  }
}

function downloadPolicyOutput() {
  const out = document.getElementById("policy_output");
  const status = document.getElementById("policy_status");
  const text = out ? out.textContent || "" : "";
  try {
    JSON.parse(text);
  } catch (_) {
    if (status) status.textContent = "Nothing to download until the policy builds without errors.";
    return;
  }
  const url = URL.createObjectURL(new Blob([text], { type: "application/json" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = "tailscale-policy.json";
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

function bindOutputActions() {
  const copyBtn = document.getElementById("copy_policy");
  const downloadBtn = document.getElementById("download_policy");

  if (copyBtn && !copyBtn.dataset.bound) {
    copyBtn.addEventListener("click", () => {
      copyPolicyOutput();
    });
    copyBtn.dataset.bound = "1";
  }

  if (downloadBtn && !downloadBtn.dataset.bound) {
    downloadBtn.addEventListener("click", downloadPolicyOutput);
    downloadBtn.dataset.bound = "1";
  }
}

function resetToDefaults() {
  try {
    localStorage.removeItem(ACL_DRAFT_STORAGE_KEY);
  } catch (_) {
    // Ignore storage failures.
  }
  applyInputs(defaultInputs);
  const status = document.getElementById("acl-draft-status");
  if (status) {
    status.textContent = "Using the default ACL suggestions until you generate a new draft.";
  }
  generatePolicy();
}

function bindAclInputEvents() {
  const generateBtn = document.getElementById("generate");
  const resetBtn = document.getElementById("reset_defaults");
  const inputsSection = document.getElementById("acl-inputs-section");

  if (generateBtn && !generateBtn.dataset.bound) {
    generateBtn.addEventListener("click", generatePolicy);
    generateBtn.dataset.bound = "1";
  }

  if (resetBtn && !resetBtn.dataset.bound) {
    resetBtn.addEventListener("click", resetToDefaults);
    resetBtn.dataset.bound = "1";
  }

  if (inputsSection && !inputsSection.dataset.bound) {
    inputsSection.addEventListener("input", (event) => {
      const target = event.target;
      if (target && ["INPUT", "TEXTAREA"].includes(target.tagName)) {
        schedulePolicy();
      }
    });
    inputsSection.addEventListener("change", (event) => {
      const target = event.target;
      if (target && (target.tagName === "SELECT" || target.type === "checkbox")) {
        schedulePolicy();
      }
    });
    inputsSection.dataset.bound = "1";
  }

  // Snapshot the server-rendered values before a saved draft overrides them, for "Reset to defaults".
  defaultInputs = collectInputs();
  restoreAclDraft();
  generatePolicy();
}

document.body.addEventListener("htmx:afterSwap", (event) => {
  const targetId = event.target && event.target.id ? event.target.id : "";
  if (targetId === "acl-inputs-section") {
    bindAclInputEvents();
  }
  if (targetId === "acl-output-section") {
    bindOutputActions();
    generatePolicy();
  }
  if (targetId === "acl-overview-section") {
    generatePolicy();
  }
});

initSidebar();
bindOutputActions();
