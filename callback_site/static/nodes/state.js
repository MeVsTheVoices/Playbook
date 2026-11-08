const body = document.body;

const toggleBtn = document.getElementById("sidebar-toggle");

const configStatus = document.getElementById("config-status");

const monitorStatus = document.getElementById("monitor-status");

const provisionStatusEl = document.getElementById("provision-status");

// Check/Apply Module sit beside #module-status, far from #provision-status, so show progress in both.
const provisionStatus = {
  set textContent(text) {
    provisionStatusEl.textContent = text;
    moduleStatusEl.textContent = text;
  },
};

const provisionLog = document.getElementById("provision-log");

const rows = document.getElementById("node-rows");

const provisionNode = document.getElementById("provision-node");

const runAnsible = document.getElementById("run-ansible");

const statNodeCountEl = document.getElementById("stat-node-count");

const statReachableCountEl = document.getElementById("stat-reachable-count");

const statCallbackAddressCountEl = document.getElementById("stat-callback-address-count");

const statCallbackCountEl = document.getElementById("stat-callback-count");

const manualNodeStatus = document.getElementById("manual-node-status");

const manualNodeSelectorEl = document.getElementById("manual-node-selector");

const manualNodeNameEl = document.getElementById("manual-node-name");

const manualNodeGroupEl = document.getElementById("manual-node-group");

const manualNodeAddressEl = document.getElementById("manual-node-address");

const manualNodeUsernameEl = document.getElementById("manual-node-username");

const manualNodePasswordEl = document.getElementById("manual-node-password");

const manualNodeShellEl = document.getElementById("manual-node-shell");

const manualNodeCertificateEl = document.getElementById("manual-node-certificate");

const profileSegmentProfileEl = document.getElementById("profile-segment-profile");

const profileSegmentNameEl = document.getElementById("profile-segment-name");

const profileSegmentConfigEl = document.getElementById("profile-segment-config");

const profileSegmentDefaultsEl = document.getElementById("profile-segment-defaults");

const profileSegmentGalaxyEl = document.getElementById("profile-segment-galaxy");

const profileSegmentStatusEl = document.getElementById("profile-segment-status");

const profileSegmentListEl = document.getElementById("profile-segment-list");

let nodes = [];

let profiles = [];

let statusByNode = {};

let nodeInputs = {};

let manualNodes = {};

let selectedModuleId = "";

const CONFIGURED_SECRET_MARKER = "__CONFIGURED__"; // keep in sync with core.py

const moduleStatusBodyEl = document.getElementById("module-status-body");

const moduleDetailEl = document.getElementById("module-detail");

const moduleStatusEl = document.getElementById("module-status");

function initSidebar() {
  const saved = localStorage.getItem("sidebar-collapsed");
  if (saved === "1") body.classList.add("sidebar-collapsed");
}
