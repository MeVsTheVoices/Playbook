function setMonitorCheckState(state, message) {
  const classes = ["status-state"];
  if (state === "busy") classes.push("busy");
  if (state === "ok") classes.push("ok");
  if (state === "error") classes.push("error");
  monitorCheckStatusEl.className = classes.join(" ");
  monitorCheckStatusEl.textContent = message;
}

function setPingVisualState(button, state, label) {
  if (!button) {
    return;
  }
  button.classList.remove("ping-pending", "ping-good", "ping-warn", "ping-bad");
  if (state) {
    button.classList.add(state);
  }
  if (label) {
    button.textContent = label;
  }
}

function resetPingButton(button) {
  if (!button) {
    return;
  }
  const pendingTimer = pingResetTimers.get(button);
  if (pendingTimer) {
    clearTimeout(pendingTimer);
    pingResetTimers.delete(button);
  }
  button.disabled = false;
  setPingVisualState(button, "", button.dataset.defaultLabel || "Simple Ping");
}

function finishPingButton(button, outcomeClass, label) {
  setPingVisualState(button, outcomeClass, label);
  const delay = PING_RESET_DELAYS_MS[outcomeClass] || PING_RESET_DELAYS_MS.default;
  const timerId = setTimeout(() => {
    resetPingButton(button);
  }, delay);
  pingResetTimers.set(button, timerId);
}

function classifyPingOutcome(data) {
  if (!data || !data.reachable) {
    return { state: "ping-bad", label: "Bad" };
  }

  const rawLatency = Number(data.latency_ms);
  if (!Number.isFinite(rawLatency)) {
    return { state: "ping-good", label: "Good" };
  }

  const latency = Math.max(0, rawLatency);
  if (latency <= 25) {
    return { state: "ping-good", label: latency < 1 ? "<1ms" : `${Math.round(latency)}ms` };
  }
  if (latency <= 90) {
    return { state: "ping-warn", label: `${Math.round(latency)}ms` };
  }
  return { state: "ping-bad", label: `${Math.round(latency)}ms` };
}

function publishPingResult(nodeName, outcome, data, triggerButton) {
  const latency = Number(data && data.latency_ms);
  const latencyText = Number.isFinite(latency)
    ? `${Math.round(Math.max(0, latency))}ms`
    : String(outcome.label || "-");
  const errorText = String((data && data.error) || "").trim();

  if (triggerButton === monitorRunPingBtn) {
    const msg = outcome.state === "ping-bad"
      ? `Ping failed for ${nodeName}.${errorText ? ` ${errorText}` : ""}`
      : `Ping ${outcome.state === "ping-warn" ? "slow" : "ok"} for ${nodeName} (${latencyText}).`;
    setMonitorCheckState(outcome.state === "ping-bad" ? "error" : "ok", msg);
    return;
  }

  if (triggerButton === nodeFormPingBtn) {
    nodeFormStatusEl.textContent = outcome.state === "ping-bad"
      ? `Ping failed for ${nodeName}.${errorText ? ` ${errorText}` : ""}`
      : `Ping ${outcome.state === "ping-warn" ? "slow" : "ok"} for ${nodeName} (${latencyText}).`;
    return;
  }

  monitorStatus.textContent = outcome.state === "ping-bad"
    ? `Ping failed for ${nodeName}.${errorText ? ` ${errorText}` : ""}`
    : `Ping ${outcome.state === "ping-warn" ? "slow" : "ok"} for ${nodeName} (${latencyText}).`;
}

async function runSimplePing(nodeName, triggerButton) {
  const selected = String(nodeName || "").trim();
  if (!triggerButton) {
    return;
  }

  const existingTimer = pingResetTimers.get(triggerButton);
  if (existingTimer) {
    clearTimeout(existingTimer);
    pingResetTimers.delete(triggerButton);
  }

  if (!selected) {
    finishPingButton(triggerButton, "ping-warn", "Pick Node");
    return;
  }

  triggerButton.disabled = true;
  setPingVisualState(triggerButton, "ping-pending", "Pinging");
  if (triggerButton === monitorRunPingBtn) {
    setMonitorCheckState("busy", `Pinging ${selected}...`);
  } else if (triggerButton === nodeFormPingBtn) {
    nodeFormStatusEl.textContent = `Pinging ${selected}...`;
  } else {
    monitorStatus.textContent = `Pinging ${selected}...`;
  }
  try {
    const res = await fetchJsonWithTimeout(`/api/monitor/nodes/${encodeURIComponent(selected)}/ping`, {}, 12000);
    const data = await res.json();
    if (!res.ok || !data.ok) {
      finishPingButton(triggerButton, "ping-bad", "Bad");
      publishPingResult(selected, { state: "ping-bad", label: "Bad" }, data || {}, triggerButton);
      return;
    }

    const outcome = classifyPingOutcome(data);
    finishPingButton(triggerButton, outcome.state, outcome.label);
    publishPingResult(selected, outcome, data, triggerButton);
  } catch (err) {
    if (err && err.name === "AbortError") {
      finishPingButton(triggerButton, "ping-warn", "Slow");
      publishPingResult(selected, { state: "ping-warn", label: "Slow" }, { error: "Request timed out" }, triggerButton);
    } else {
      finishPingButton(triggerButton, "ping-bad", "Bad");
      publishPingResult(selected, { state: "ping-bad", label: "Bad" }, { error: String(err && err.message ? err.message : err) }, triggerButton);
    }
  }
}
