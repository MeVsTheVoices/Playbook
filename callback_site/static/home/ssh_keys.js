function refreshKeyLinks(forcedKeyName) {
  const keyNameInput = (forcedKeyName || document.getElementById("ssh_key_name").value).trim();
  if (!keyNameInput) {
    keyLinksEl.innerHTML = "";
    return;
  }
  const keyName = encodeURIComponent(keyNameInput);
  keyLinksEl.innerHTML =
    `<a href="/api/ssh/download/${keyName}/private">Download private key</a>` +
    `<a href="/api/ssh/download/${keyName}/public">Download public key</a>` +
    `<a href="/api/ssh/public/${keyName}">Public-key endpoint</a>`;
}

async function refreshSshKeyChoices(selectedKeyName = "") {
  const res = await fetch("/api/ssh/keys");
  const data = await res.json();
  if (!res.ok || !data.ok) {
    sshKeyStatusEl.textContent = data.error || "Could not load saved SSH keys.";
    return;
  }

  const previous = selectedKeyName || sshKeySelectEl.value || "";
  sshKeySelectEl.innerHTML = "";

  const empty = document.createElement("option");
  empty.value = "";
  empty.textContent = "No saved key selected";
  sshKeySelectEl.appendChild(empty);

  for (const key of Array.isArray(data.keys) ? data.keys : []) {
    const option = document.createElement("option");
    option.value = key.key_name;
    option.textContent = key.public_key_exists ? key.key_name : `${key.key_name} (missing public key)`;
    sshKeySelectEl.appendChild(option);
  }

  if (previous) {
    sshKeySelectEl.value = previous;
  }
}

async function loadSelectedSshKey(retainSelection = true) {
  const keyName = String(sshKeySelectEl.value || "").trim();
  if (!keyName) {
    sshKeyStatusEl.textContent = "Select a saved key first.";
    return;
  }

  const res = await fetch(`/api/ssh/public/${encodeURIComponent(keyName)}`);
  if (!res.ok) {
    sshKeyStatusEl.textContent = "Could not load the selected key.";
    return;
  }

  form.ssh_public_key.value = (await res.text()).trim();
  form.ssh_public_key.dataset.keyName = keyName;
  document.getElementById("ssh_key_name").value = keyName;
  refreshKeyLinks(keyName);
  sshKeyStatusEl.textContent = `Loaded saved key '${keyName}' into SSH public key.`;
  if (retainSelection) {
    try {
      await saveSdBrowserSelection({ssh_key_name: keyName});
    } catch (error) {
      sshKeyStatusEl.textContent = `Key loaded, but session selection could not be retained: ${error.message}`;
    }
  }
}

function handleSshKeyUpload() {
  const file = sshKeyUploadEl.files && sshKeyUploadEl.files[0];
  if (!file) {
    sshKeyStatusEl.textContent = "Choose a .pub file to upload.";
    return;
  }

  const reader = new FileReader();
  reader.onload = () => {
    const value = String(reader.result || "").trim();
    if (!value) {
      sshKeyStatusEl.textContent = "Uploaded file was empty.";
      return;
    }
    form.ssh_public_key.value = value;
    delete form.ssh_public_key.dataset.keyName;
    saveSdBrowserSelection({ssh_key_name: ""}).catch(error => {
      sshKeyStatusEl.textContent = `Uploaded key loaded, but session selection could not be cleared: ${error.message}`;
    });
    sshKeyStatusEl.textContent = `Loaded uploaded key file '${file.name}' into SSH public key.`;
  };
  reader.onerror = () => {
    sshKeyStatusEl.textContent = "Could not read the uploaded file.";
  };
  reader.readAsText(file);
}

async function runSshKeyAction(mode) {
  const keyNameInput = document.getElementById("ssh_key_name").value.trim();
  const verb = mode === "generate" ? "Generating" : "Loading";
  sshKeyStatusEl.textContent = `${verb} key...`;
  const res = await fetch("/api/ssh/generate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key_name: keyNameInput, comment: "rack-bootstrap", mode }),
  });
  const data = await res.json();
  if (!res.ok || !data.ok) {
    sshKeyStatusEl.textContent = (data.errors || ["SSH key action failed"]).join("; ");
    return;
  }
  form.ssh_public_key.value = data.public_key;
  form.ssh_public_key.dataset.keyName = data.key_name;
  refreshKeyLinks(data.key_name);
  const modeLabel = (data.mode || mode) === "generate" ? "Generate" : "Load";
  sshKeyStatusEl.textContent = `${data.created ? "Created" : "Loaded"} key '${data.key_name}' using ${modeLabel} mode. Public key inserted. Private key: ${data.private_key_path}`;
  try {
    await saveSdBrowserSelection({ssh_key_name: data.key_name});
  } catch (error) {
    sshKeyStatusEl.textContent = `Key ready, but session selection could not be retained: ${error.message}`;
  }
}
