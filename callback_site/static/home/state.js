const body = document.body;

const toggleBtn = document.getElementById("sidebar-toggle");

const form = document.getElementById("sd-form");

const flashMascotEl = document.getElementById("flash-mascot");

const statusEl = document.getElementById("job-status");

const logEl = document.getElementById("job-log");

const eventsBodyEl = document.getElementById("events-body");

const sshKeyStatusEl = document.getElementById("ssh-key-status");

const keyLinksEl = document.getElementById("key-links");

const osPresetNoteEl = document.getElementById("os-preset-note");

const catalogMetaEl = document.getElementById("catalog-meta");

const deviceWebhookStatusEl = document.getElementById("device-webhook-status");

const sdOptionsProfileSelectEl = document.getElementById("sd_options_profile_select");

const sdOptionsProfileNameEl = document.getElementById("sd_options_profile_name");

const sdOptionsProfileDescriptionEl = document.getElementById("sd_options_profile_description");

const sdOptionsProfilePiOnlyEl = document.getElementById("sd_options_profile_pi_only");

const sdOptionsProfileStatusEl = document.getElementById("sd-options-profile-status");

const sdOptionsConfigCountEl = document.getElementById("sd-options-config-count");

const sdOptionsLoadBtn = document.getElementById("sd-options-load");

const sdOptionsSaveBtn = document.getElementById("sd-options-save");

const sdOptionsResetBtn = document.getElementById("sd-options-reset");

const sdOptionsSecurityDefaultsBtn = document.getElementById("sd-options-security-defaults");

const sdOptionsDeleteBtn = document.getElementById("sd-options-delete");

const sdOptionsInputEls = Array.from(document.querySelectorAll("[data-option-key]"));

const sshKeySelectEl = document.getElementById("ssh_key_select");

const sshKeyUploadEl = document.getElementById("ssh_key_upload");

const eventSearchEl = document.getElementById("event-search");

const eventProfileEl = document.getElementById("event-profile");

const eventStageEl = document.getElementById("event-stage");

const eventMetaEl = document.getElementById("event-meta");

const statVisibleEl = document.getElementById("stat-visible");

const statTotalEl = document.getElementById("stat-total");

const stat24hEl = document.getElementById("stat-24h");

const statHostsEl = document.getElementById("stat-hosts");

const activitySdCardEl = document.getElementById("activity-sd-card");

const activitySdStateEl = document.getElementById("activity-sd-state");

const activitySdDetailEl = document.getElementById("activity-sd-detail");

const activityCallbackCardEl = document.getElementById("activity-callback-card");

const activityCallbackStateEl = document.getElementById("activity-callback-state");

const activityCallbackDetailEl = document.getElementById("activity-callback-detail");

const activityDeviceCardEl = document.getElementById("activity-device-card");

const activityDeviceStateEl = document.getElementById("activity-device-state");

const activityDeviceDetailEl = document.getElementById("activity-device-detail");

const activityBannerEl = document.getElementById("activity-banner");

const SD_ACTIVE_JOB_KEY = "sd-active-job-id";

const SD_HOSTNAME_INCREMENTED_JOB_KEY = "sd-hostname-incremented-job-id";

const NATO_SEQUENCE = [
  "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india", "juliet", "kilo", "lima",
  "mike", "november", "oscar", "papa", "quebec", "romeo", "sierra", "tango", "uniform", "victor", "whiskey", "xray", "yankee", "zulu",
];

let currentJob = null;

let deviceWebhookRetry = null;

let deviceFallbackPollTimer = null;

let allEvents = [];

let sdOptionProfiles = initialSdOptionProfiles || {};

let sdOptionProfileOrder = Object.keys(sdOptionProfiles || {});

let activityMood = {
  sd: "idle",
  callback: "quiet",
  device: "connecting",
};
