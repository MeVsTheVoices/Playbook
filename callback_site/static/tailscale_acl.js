const body = document.body;
const toggleBtn = document.getElementById("sidebar-toggle");
const ACL_DRAFT_STORAGE_KEY = "tailscale-acl-last-used-v1";

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

function parseSubnets(text) {
  return text
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean);
}

function setTailscaleSettingsStatus(message) {
  const status = document.getElementById("tailscale_settings_status");
  if (status) {
    status.textContent = message;
  }
}

function setTailscaleApiKeyHint(message) {
  const hint = document.getElementById("tailscale_api_key_hint");
  if (hint) {
    hint.textContent = message;
  }
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

function saveAclDraft() {
  const rackNameEl = document.getElementById("rack_name");
  const tailnetEl = document.getElementById("tailnet");
  const adminEl = document.getElementById("admin_group");
  const rpi5El = document.getElementById("rpi5_tag");
  const threeBplusEl = document.getElementById("rpi_3bplus_tag");
  const homelabEl = document.getElementById("homelab_tag");
  const subnetEl = document.getElementById("pi5_subnets");

  if (!rackNameEl || !tailnetEl || !adminEl || !rpi5El || !homelabEl || !subnetEl) {
    return;
  }

  const draft = {
    rack_name: rackNameEl.value,
    tailnet: tailnetEl.value,
    admin_group: adminEl.value,
    rpi5_tag: rpi5El.value,
    rpi_3bplus_tag: threeBplusEl ? threeBplusEl.value : "",
    homelab_tag: homelabEl.value,
    pi5_subnets: subnetEl.value,
  };

  try {
    localStorage.setItem(ACL_DRAFT_STORAGE_KEY, JSON.stringify(draft));
  } catch (_) {
    // Ignore storage quota or privacy mode failures.
  }
}

function applySettingsDefault(element, value, hasLocalDraft) {
  if (!element || !value || hasLocalDraft) {
    return;
  }
  if (!String(element.value || "").trim()) {
    element.value = value;
  }
}

async function loadTailscaleSettings(hasLocalDraft = false) {
  const tailnetEl = document.getElementById("tailnet");
  const rackNameEl = document.getElementById("rack_name");
  if (!tailnetEl) {
    return;
  }

  setTailscaleSettingsStatus("Loading Tailscale settings...");
  try {
    const res = await fetch("/api/settings");
    const data = await res.json();
    if (!res.ok || !data.ok) {
      setTailscaleSettingsStatus(data.error || "Failed to load Tailscale settings.");
      return;
    }

    const settings = data.settings || {};
    const tailnet = String(settings.tailscale_tailnet || "").trim();
    const rackName = String(settings.rack_name || "").trim();
    const apiKeySet = Boolean(settings.tailscale_api_key_set);
    applySettingsDefault(rackNameEl, rackName, hasLocalDraft);
    applySettingsDefault(tailnetEl, tailnet, hasLocalDraft);
    setTailscaleApiKeyHint(apiKeySet ? "API key saved in settings." : "No API key saved yet.");
    setTailscaleSettingsStatus("Tailscale settings loaded.");
  } catch (_) {
    setTailscaleSettingsStatus("Failed to load Tailscale settings.");
  }
}

async function saveTailscaleSettings() {
  const tailnetEl = document.getElementById("tailnet");
  const apiKeyEl = document.getElementById("tailscale_api_key");
  if (!tailnetEl || !apiKeyEl) {
    return;
  }

  setTailscaleSettingsStatus("Saving Tailscale settings...");
  try {
    const res = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tailscale_tailnet: tailnetEl.value.trim(),
        tailscale_api_key: apiKeyEl.value,
      }),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      setTailscaleSettingsStatus(data.error || "Failed to save Tailscale settings.");
      return;
    }

    const saved = data.settings || {};
    tailnetEl.value = String(saved.tailscale_tailnet || tailnetEl.value || "").trim();
    const apiKeySet = Boolean(saved.tailscale_api_key_set) || Boolean(String(saved.tailscale_api_key || "").trim());
    apiKeyEl.value = "";
    setTailscaleApiKeyHint(apiKeySet ? "API key saved in settings." : "No API key saved yet.");
    setTailscaleSettingsStatus("Tailscale settings saved.");
    generatePolicy();
  } catch (_) {
    setTailscaleSettingsStatus("Failed to save Tailscale settings.");
  }
}

function applyAclDraft(draft) {
  if (!draft || typeof draft !== "object") {
    return false;
  }

  const fields = [
    ["rack_name", document.getElementById("rack_name")],
    ["tailnet", document.getElementById("tailnet")],
    ["admin_group", document.getElementById("admin_group")],
    ["rpi5_tag", document.getElementById("rpi5_tag")],
    ["rpi_3bplus_tag", document.getElementById("rpi_3bplus_tag")],
    ["homelab_tag", document.getElementById("homelab_tag")],
    ["pi5_subnets", document.getElementById("pi5_subnets")],
  ];

  let applied = false;
  for (const [key, element] of fields) {
    if (!element || draft[key] === undefined || draft[key] === null) continue;
    element.value = String(draft[key]);
    applied = true;
  }
  return applied;
}

function restoreAclDraft() {
  const status = document.getElementById("acl-draft-status");
  const draft = readAclDraft();
  if (applyAclDraft(draft)) {
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

function generatePolicy() {
  const out = document.getElementById("policy_output");
  const status = document.getElementById("policy_status");
  const rackNameEl = document.getElementById("rack_name");
  const tailnetEl = document.getElementById("tailnet");
  const adminEl = document.getElementById("admin_group");
  const rpi5El = document.getElementById("rpi5_tag");
  const threeBplusEl = document.getElementById("rpi_3bplus_tag");
  const homelabEl = document.getElementById("homelab_tag");
  const subnetEl = document.getElementById("pi5_subnets");

  if (!out || !rackNameEl || !tailnetEl || !adminEl || !rpi5El || !homelabEl || !subnetEl) {
    return;
  }

  const rackNameRaw = rackNameEl.value.trim().toLowerCase();
  const rackName = rackNameRaw.replace(/[^a-z0-9-]+/g, "-").replace(/^-+|-+$/g, "") || "homelab";
  const suggestedHomelabTag = `tag:${rackName}`;
  if (!homelabEl.value.trim() || homelabEl.value.trim() === "tag:homelab") {
    homelabEl.value = suggestedHomelabTag;
  }

  const admin = adminEl.value.trim();
  const rpi5Tag = rpi5El.value.trim();
  const threeBplusTag = threeBplusEl ? threeBplusEl.value.trim() : "";
  const homelabTag = homelabEl.value.trim();
  const subnets = parseSubnets(subnetEl.value);
  const piNodeTags = [rpi5Tag, threeBplusTag].filter(Boolean);

  const policy = {
    tagOwners: {
      [rpi5Tag]: [admin],
      ...(threeBplusTag ? { [threeBplusTag]: [admin] } : {}),
      [homelabTag]: [admin],
    },
    autoApprovers: {
      routes: {},
    },
    acls: [
      {
        action: "accept",
        src: [admin],
        dst: [...piNodeTags.map((tag) => `${tag}:*`), `${homelabTag}:*`],
      },
      {
        action: "accept",
        src: piNodeTags,
        dst: [`${homelabTag}:*`],
      },
    ],
    ssh: [
      {
        action: "accept",
        src: [admin],
        dst: piNodeTags,
        users: ["autogroup:nonroot", "root"],
      },
    ],
  };

  subnets.forEach((cidr) => {
    policy.autoApprovers.routes[cidr] = [rpi5Tag, homelabTag];
  });

  out.textContent = JSON.stringify(policy, null, 2);
  if (status) {
    const now = new Date();
    const stamp = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    status.textContent = `Updated ${stamp} - ${subnets.length} route${subnets.length === 1 ? "" : "s"}`;
  }

  saveAclDraft();

  syncOverview({
    rack: rackNameEl.value.trim() || "Not set",
    tailnet: tailnetEl.value.trim() || "Not set",
    routes: subnets.length,
  });
  refreshFieldCards();
  updateSubnetCounter(subnets.length);
}

function syncOverview(data) {
  const rackEl = document.getElementById("ov-rack");
  const tailnetEl = document.getElementById("ov-tailnet");
  const routeEl = document.getElementById("ov-route-count");
  if (rackEl) rackEl.textContent = data.rack;
  if (tailnetEl) tailnetEl.textContent = data.tailnet;
  if (routeEl) routeEl.textContent = String(data.routes);
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
    const input = card.querySelector("input, textarea");
    if (!input) {
      return;
    }
    card.classList.toggle("field-ready", !!input.value.trim());
  });
}

function formatPolicyOutput() {
  const out = document.getElementById("policy_output");
  if (!out) {
    return;
  }
  try {
    const parsed = JSON.parse(out.textContent || "{}");
    out.textContent = JSON.stringify(parsed, null, 2);
  } catch (error) {
    // Keep existing content if output is not parseable.
  }
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

function bindOutputActions() {
  const copyBtn = document.getElementById("copy_policy");
  const formatBtn = document.getElementById("format_policy");

  if (copyBtn && !copyBtn.dataset.bound) {
    copyBtn.addEventListener("click", () => {
      copyPolicyOutput();
    });
    copyBtn.dataset.bound = "1";
  }

  if (formatBtn && !formatBtn.dataset.bound) {
    formatBtn.addEventListener("click", () => {
      formatPolicyOutput();
    });
    formatBtn.dataset.bound = "1";
  }
}

function bindAclInputEvents() {
  const generateBtn = document.getElementById("generate");
  const inputsSection = document.getElementById("acl-inputs-section");
  const saveTailscaleSettingsBtn = document.getElementById("save_tailscale_settings");

  if (generateBtn && !generateBtn.dataset.bound) {
    generateBtn.addEventListener("click", generatePolicy);
    generateBtn.dataset.bound = "1";
  }

  if (inputsSection && !inputsSection.dataset.bound) {
    inputsSection.addEventListener("input", (event) => {
      const target = event.target;
      if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA")) {
        generatePolicy();
      }
    });
    inputsSection.dataset.bound = "1";
  }

  if (saveTailscaleSettingsBtn && !saveTailscaleSettingsBtn.dataset.bound) {
    saveTailscaleSettingsBtn.addEventListener("click", () => {
      saveTailscaleSettings();
    });
    saveTailscaleSettingsBtn.dataset.bound = "1";
  }

  const hasLocalDraft = restoreAclDraft();
  loadTailscaleSettings(hasLocalDraft).finally(() => {
    generatePolicy();
  });
}

document.body.addEventListener("htmx:afterSwap", (event) => {
  const targetId = event.target && event.target.id ? event.target.id : "";
  if (targetId === "acl-inputs-section" || targetId === "acl-output-section") {
    bindAclInputEvents();
    bindOutputActions();
  }
  if (targetId === "acl-overview-section") {
    generatePolicy();
  }
});

initSidebar();
bindOutputActions();
