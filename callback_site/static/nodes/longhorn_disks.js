let longhornDiskRequestId = 0;

function longhornPlanText(plan, path) {
  if (!plan) return "No plan reported.";
  if (plan.error) return `Applying Longhorn would stop: ${plan.error}`;
  const verb = {present: "keep using", mount: "mount", format: "partition and format"}[plan.action] || plan.action;
  return `Applying Longhorn would ${verb} ${plan.disk} at ${path}.`;
}

async function checkLonghornDisks(button, status, output) {
  const node = provisionNode.value;
  const requestId = ++longhornDiskRequestId;
  output.replaceChildren();
  if (!node) {
    status.textContent = "Select a node first.";
    return;
  }
  const field = (id) => (moduleDetailEl.querySelector(`[data-module-field='${id}']`) || {}).value || "";
  const query = new URLSearchParams({node, path: field("storage_path"), device: field("storage_disk")});
  button.disabled = true;
  status.textContent = `Asking ${node} which disks Longhorn could use...`;
  try {
    const response = await fetchJsonWithTimeout(`/api/nodes/longhorn-disks?${query}`, {cache: "no-store"}, 160000);
    const data = await response.json();
    if (requestId !== longhornDiskRequestId) return;
    if (!response.ok || !data.ok) throw new Error(data.error || `HTTP ${response.status}`);
    const table = document.createElement("table");
    const head = table.createTHead().insertRow();
    for (const title of ["Disk", "Size", "Model", "Bus", "Longhorn"]) {
      const th = document.createElement("th");
      th.textContent = title;
      head.appendChild(th);
    }
    const body = table.createTBody();
    for (const disk of data.disks) {
      const row = body.insertRow();
      row.className = disk.usable ? "longhorn-disk-usable" : "longhorn-disk-excluded";
      const cells = [
        disk.name,
        `${(disk.size_bytes / 1024 ** 3).toFixed(1)} GiB`,
        disk.model || "-",
        disk.transport || (disk.nvme ? "nvme" : "-"),
        `${disk.usable ? "usable" : "not used"}: ${disk.reason}`,
      ];
      for (const text of cells) row.insertCell().textContent = text;
    }
    if (!data.disks.length) body.insertRow().insertCell().textContent = "No disks reported.";
    const plan = document.createElement("p");
    plan.className = "module-callout";
    plan.textContent = longhornPlanText(data.plan, data.path);
    output.append(plan, table);
    status.textContent = `Read by Ansible (rack_storage_disk, report only) from ${data.target} at ${data.read_at}.`;
  } catch (error) {
    if (requestId !== longhornDiskRequestId) return;
    status.textContent = `Could not check disks on ${node}: ${error.message || error}`;
  } finally {
    if (requestId === longhornDiskRequestId) button.disabled = false;
  }
}

function appendLonghornDiskCheck(parent) {
  longhornDiskRequestId += 1;
  const box = document.createElement("div");
  box.id = "longhorn-disks";
  const button = document.createElement("button");
  button.type = "button";
  button.className = "secondary";
  button.id = "longhorn-disks-check";
  button.textContent = "Check disks";
  const status = document.createElement("p");
  status.className = "module-hint";
  status.setAttribute("role", "status");
  status.textContent = "Lists the node's block devices, which ones Longhorn could use, and what applying would do. Changes nothing.";
  const output = document.createElement("div");
  output.id = "longhorn-disks-result";
  button.addEventListener("click", () => checkLonghornDisks(button, status, output));
  box.append(button, status, output);
  parent.appendChild(box);
}
