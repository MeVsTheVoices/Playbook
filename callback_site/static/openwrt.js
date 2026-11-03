const body = document.body;
const toggleBtn = document.getElementById('sidebar-toggle');
const inspectBtn = document.getElementById('inspect-btn');
const pushKeyBtn = document.getElementById('push-key-btn');
const resultStatus = document.getElementById('result-status');
const resultBody = document.getElementById('result-body');
const activity = document.getElementById('router-activity');
const keySelect = document.getElementById('ssh-key-select');
const formFields = [
  ['host', 'host'],
  ['port', 'port'],
  ['username', 'username'],
  ['password', 'password'],
  ['connection', 'connection'],
  ['module-hint', 'module_hint'],
  ['become', 'become'],
  ['push-key-host', 'push_key_host'],
  ['push-key-port', 'push_key_port'],
  ['push-key-user', 'push_key_user'],
  ['push-key-password', 'push_key_password'],
];
const STORAGE_KEY = 'openwrt-form:';

function initSidebar() {
  const saved = localStorage.getItem('sidebar-collapsed');
  if (saved === '1') {
    body.classList.add('sidebar-collapsed');
  }
}

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function getStorageKey() {
  const rackName = document.body.getAttribute('data-rack-name') || 'default';
  return `${STORAGE_KEY}${rackName}`;
}

function readStoredState() {
  try {
    const raw = localStorage.getItem(getStorageKey());
    return raw ? JSON.parse(raw) : {};
  } catch (error) {
    return {};
  }
}

function writeStoredState() {
  const state = {};
  for (const [id, key] of formFields) {
    const element = document.getElementById(id);
    if (!element) continue;
    if (element.type === 'checkbox') {
      state[key] = element.checked;
    } else {
      state[key] = element.value;
    }
  }
  state.ssh_key_name = keySelect.value;
  localStorage.setItem(getStorageKey(), JSON.stringify(state));
}

function restoreStoredState() {
  const state = readStoredState();
  for (const [id, key] of formFields) {
    const element = document.getElementById(id);
    if (!element) continue;
    const value = state[key];
    if (typeof value === 'undefined') continue;
    if (element.type === 'checkbox') {
      element.checked = Boolean(value);
    } else {
      element.value = value;
    }
  }
  if (state.ssh_key_name) {
    keySelect.value = state.ssh_key_name;
  }
}

async function loadSavedKeys() {
  try {
    const response = await fetch('/api/ssh/keys');
    const data = await response.json();
    if (!data.ok) {
      return;
    }
    const keys = data.keys || [];
    keySelect.innerHTML = '<option value="">No saved key selected</option>';
    for (const item of keys) {
      const option = document.createElement('option');
      option.value = item.key_name;
      option.textContent = item.key_name;
      keySelect.appendChild(option);
    }
  } catch (error) {
    console.error(error);
  }
}

for (const [id] of formFields) {
  const element = document.getElementById(id);
  if (!element) continue;
  element.addEventListener('input', writeStoredState);
  element.addEventListener('change', writeStoredState);
}
keySelect.addEventListener('change', writeStoredState);

toggleBtn.addEventListener('click', () => {
  body.classList.toggle('sidebar-collapsed');
  localStorage.setItem('sidebar-collapsed', body.classList.contains('sidebar-collapsed') ? '1' : '0');
});

inspectBtn.addEventListener('click', async () => {
  const payload = {
    host: document.getElementById('host').value.trim(),
    port: Number(document.getElementById('port').value || 22),
    username: document.getElementById('username').value.trim(),
    password: document.getElementById('password').value,
    connection: document.getElementById('connection').value,
    module_hint: document.getElementById('module-hint').value.trim(),
    become: document.getElementById('become').checked,
    ssh_key_name: keySelect.value,
  };

  activity.classList.add('busy');
  resultStatus.textContent = 'Inspecting...';
  resultBody.innerHTML = '';

  try {
    const response = await fetch('/api/openwrt/inspect', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    let data = null;
    try {
      data = await response.json();
    } catch (error) {
      data = { ok: false, error: `Server did not return JSON (${response.status}).` };
    }
    if (!response.ok || !data || !data.ok) {
      activity.classList.remove('busy');
      resultStatus.textContent = data?.error || 'Inspection failed.';
      return;
    }

    const stats = data.stats || {};
    const probe = data.probe || {};
    activity.classList.remove('busy');
    resultStatus.innerHTML = `Module <strong>${escapeHtml(data.module)}</strong> • ${escapeHtml(stats.summary)}`;
    resultBody.innerHTML = `
    <div class="result-card">
      <div class="result-stat-grid">
        <div class="result-metric"><strong>Host</strong>${escapeHtml(data.request.host)}</div>
        <div class="result-metric"><strong>Port</strong>${escapeHtml(String(stats.port))}</div>
        <div class="result-metric"><strong>Username</strong>${escapeHtml(data.request.username)}</div>
        <div class="result-metric"><strong>Connection</strong>${escapeHtml(stats.connection)}</div>
        <div class="result-metric"><strong>Become</strong>${escapeHtml(stats.become ? 'yes' : 'no')}</div>
        <div class="result-metric"><strong>Password</strong>${escapeHtml(stats.uses_password ? 'yes' : 'no')}</div>
        <div class="result-metric"><strong>SSH key</strong>${escapeHtml(stats.uses_ssh_key ? 'yes' : 'no')}</div>
        <div class="result-metric"><strong>Key name</strong>${escapeHtml(stats.ssh_key_name || 'none')}</div>
      </div>
      <div class="result-section">
        <div class="result-section-title">SSH probe</div>
        <div class="result-chip">${probe.ok ? 'Connected' : 'Connection failed'}</div>
        ${probe.error ? `<div class="note" style="margin-top:0.35rem;">${escapeHtml(probe.error)}</div>` : ''}
        ${probe.details ? `<pre class="result-echo">${escapeHtml(probe.details)}</pre>` : ''}
      </div>
      <div class="result-section">
        <div class="result-section-title">Inventory preview</div>
        <pre class="result-echo">${escapeHtml(data.inventory_preview)}</pre>
      </div>
    </div>
  `;
  } catch (error) {
    activity.classList.remove('busy');
    resultStatus.textContent = error?.message || 'Inspection failed.';
  }
});

pushKeyBtn.addEventListener('click', async () => {
  const payload = {
    host: document.getElementById('push-key-host').value.trim() || document.getElementById('host').value.trim(),
    port: Number(document.getElementById('push-key-port').value || 22),
    username: document.getElementById('push-key-user').value.trim() || document.getElementById('username').value.trim(),
    password: document.getElementById('push-key-password').value,
    key_name: keySelect.value,
  };
  if (!payload.key_name) {
    resultStatus.textContent = 'Select a saved key before pushing it.';
    return;
  }
  if (!payload.host) {
    resultStatus.textContent = 'Add a router host before pushing the key.';
    return;
  }

  activity.classList.add('busy');
  resultStatus.textContent = 'Pushing key...';
  resultBody.innerHTML = '';

  const response = await fetch('/api/openwrt/push-key', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  activity.classList.remove('busy');
  if (!data.ok) {
    resultStatus.textContent = data.error || 'Key push failed.';
    return;
  }
  resultStatus.innerHTML = `Key push complete for <strong>${data.key_name}</strong>.<br><span class="note">${data.details || 'The public key was pushed to the target router.'}</span>`;
});

initSidebar();
restoreStoredState();
loadSavedKeys();
