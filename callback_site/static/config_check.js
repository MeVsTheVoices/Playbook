(function () {
  const ENDPOINT = "/api/config/check";
  const COLORS = { error: "#b91c1c", warning: "#b45309", ok: "#15803d" };

  function attachLongformCheck(el, kind) {
    const checkKind = kind || (el && el.dataset.longformCheck);
    if (!el || !checkKind || el.dataset.longformAttached === "1") return;
    el.dataset.longformAttached = "1";

    const status = document.createElement("p");
    status.className = "segment-hint";
    status.setAttribute("aria-live", "polite");
    el.insertAdjacentElement("afterend", status);

    function show(level, message) {
      status.style.color = level ? COLORS[level] : "";
      status.textContent = message;
    }

    let latest = 0;
    async function run() {
      const ticket = ++latest;
      const text = el.value;
      if (!text.trim()) {
        show("", "");
        return;
      }
      try {
        const res = await fetch(ENDPOINT, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ kind: checkKind, text }),
        });
        const data = await res.json();
        if (ticket !== latest) return;
        const errors = data.errors || [];
        const warnings = data.warnings || [];
        if (!data.ok) show("error", `Syntax check failed: ${errors.join(" ")}`);
        else if (warnings.length) show("warning", `Syntax OK, but: ${warnings.join(" ")}`);
        else show("ok", "Syntax OK.");
      } catch (_) {
        if (ticket === latest) show("warning", "Syntax check unavailable; the server will re-check on save.");
      }
    }

    el.addEventListener("blur", run);
  }

  window.attachLongformCheck = attachLongformCheck;
  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-longform-check]").forEach((el) => attachLongformCheck(el));
  });
})();
