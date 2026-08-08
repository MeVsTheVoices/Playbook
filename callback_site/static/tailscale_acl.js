const body = document.body;
const toggleBtn = document.getElementById("sidebar-toggle");

function initSidebar() {
  const saved = localStorage.getItem("sidebar-collapsed");
  if (saved === "1") {
    body.classList.add("sidebar-collapsed");
  }
}

toggleBtn.addEventListener("click", () => {
  body.classList.toggle("sidebar-collapsed");
  localStorage.setItem("sidebar-collapsed", body.classList.contains("sidebar-collapsed") ? "1" : "0");
});

function parseSubnets(text) {
  return text
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean);
}

function generatePolicy() {
  const out = document.getElementById("policy_output");
  const rackNameEl = document.getElementById("rack_name");
  const adminEl = document.getElementById("admin_group");
  const rpi5El = document.getElementById("rpi5_tag");
  const bplusEl = document.getElementById("rpi_bplus_tag");
  const homelabEl = document.getElementById("homelab_tag");
  const subnetEl = document.getElementById("pi5_subnets");

  if (!out || !rackNameEl || !adminEl || !rpi5El || !bplusEl || !homelabEl || !subnetEl) {
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
  const bplusTag = bplusEl.value.trim();
  const homelabTag = homelabEl.value.trim();
  const subnets = parseSubnets(subnetEl.value);

  const policy = {
    tagOwners: {
      [rpi5Tag]: [admin],
      [bplusTag]: [admin],
      [homelabTag]: [admin],
    },
    autoApprovers: {
      routes: {},
    },
    acls: [
      {
        action: "accept",
        src: [admin],
        dst: [`${rpi5Tag}:*`, `${bplusTag}:*`, `${homelabTag}:*`],
      },
      {
        action: "accept",
        src: [rpi5Tag, bplusTag],
        dst: [`${homelabTag}:*`],
      },
    ],
    ssh: [
      {
        action: "accept",
        src: [admin],
        dst: [rpi5Tag, bplusTag],
        users: ["autogroup:nonroot", "root"],
      },
    ],
  };

  subnets.forEach((cidr) => {
    policy.autoApprovers.routes[cidr] = [rpi5Tag, homelabTag];
  });

  out.textContent = JSON.stringify(policy, null, 2);
}

function bindAclInputEvents() {
  const generateBtn = document.getElementById("generate");
  const inputsSection = document.getElementById("acl-inputs-section");

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

  generatePolicy();
}

document.body.addEventListener("htmx:afterSwap", (event) => {
  const targetId = event.target && event.target.id ? event.target.id : "";
  if (targetId === "acl-inputs-section" || targetId === "acl-output-section") {
    bindAclInputEvents();
  }
});

initSidebar();
