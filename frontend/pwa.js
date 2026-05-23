// Service worker registration + Android Chrome's beforeinstallprompt.
// iOS Safari doesn't fire beforeinstallprompt — users install via the Share
// sheet manually; we surface a one-time hint there instead.
//
// The Install action lives inside the avatar dropdown menu (rendered in
// app.js renderUserInfo). This file owns the prompt lifecycle and exposes
// state on window.__installState; app.js reads that state when it builds
// each menu and re-renders when it hears the 'install:available' event.

(function () {
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
      navigator.serviceWorker
        .register("/sw.js", { scope: "/" })
        .then((reg) => {
          // updatefound fires when the browser sees a new sw.js byte-for-byte
          // (bump_sw_version.py changes CACHE_VERSION on every deploy). The
          // incoming worker transitions installing → installed → activated.
          // If there was already a controller, this is a real update, not the
          // first install — surface a one-tap refresh prompt.
          reg.addEventListener("updatefound", () => {
            const incoming = reg.installing;
            if (!incoming) return;
            incoming.addEventListener("statechange", () => {
              if (
                incoming.state === "installed" &&
                navigator.serviceWorker.controller
              ) {
                showUpdatePrompt();
              }
            });
          });
          // Re-check for updates whenever the tab becomes visible (installed
          // PWAs sit in the background for hours — without this nudge they'd
          // only re-check on a fresh launch).
          document.addEventListener("visibilitychange", () => {
            if (document.visibilityState === "visible") {
              reg.update().catch(() => {});
            }
          });
        })
        .catch((err) => {
          console.warn("SW registration failed:", err);
        });
    });
  }

  const ua = navigator.userAgent || "";
  const isIOS = /iPad|iPhone|iPod/.test(ua) && !window.MSStream;
  const standalone =
    window.matchMedia("(display-mode: standalone)").matches ||
    window.navigator.standalone === true;

  // Shared state read by app.js when building the avatar menu.
  window.__installState = {
    available: false,        // Android Chrome / Edge fired beforeinstallprompt
    prompt: null,            // the captured event, replayed on click
    iosHint: isIOS && !standalone, // iOS path: show "Add to Home Screen" tip
    standalone,
  };

  function notify() {
    window.dispatchEvent(new CustomEvent("install:available"));
  }

  // Android Chrome / Edge.
  window.addEventListener("beforeinstallprompt", (e) => {
    e.preventDefault();
    window.__installState.prompt = e;
    window.__installState.available = true;
    notify();
  });

  // App was just installed — drop the menu item.
  window.addEventListener("appinstalled", () => {
    window.__installState.prompt = null;
    window.__installState.available = false;
    window.__installState.iosHint = false;
    notify();
  });

  // Anyone tapping an [data-action="install"] element (the avatar menu item)
  // triggers the native prompt — or, on iOS, a transient hint toast.
  document.addEventListener("click", async (e) => {
    const trigger = e.target.closest('[data-action="install"]');
    if (!trigger) return;
    e.preventDefault();
    const s = window.__installState;
    if (s.prompt) {
      trigger.disabled = true;
      try {
        s.prompt.prompt();
        await s.prompt.userChoice;
      } finally {
        s.prompt = null;
        s.available = false;
        notify();
      }
      return;
    }
    if (s.iosHint) {
      // No programmatic prompt on iOS — show a brief hint and let the user
      // open the Share sheet.
      showHint("Share → Add to Home Screen");
      return;
    }
    showHint("Use your browser menu to install");
  });

  function showUpdatePrompt() {
    if (document.getElementById("__update_toast__")) return;
    const toast = document.createElement("button");
    toast.id = "__update_toast__";
    toast.type = "button";
    toast.textContent = "Update available — tap to refresh";
    toast.style.cssText = [
      "position: fixed",
      "left: 50%",
      "bottom: max(16px, env(safe-area-inset-bottom))",
      "transform: translateX(-50%)",
      "z-index: 3000",
      "padding: 10px 16px",
      "background: #26a69a",
      "color: #0f1218",
      "border: 0",
      "border-radius: 24px",
      "font: 600 13px/1 system-ui, -apple-system, Segoe UI, Roboto, sans-serif",
      "box-shadow: 0 6px 18px rgba(0,0,0,0.45)",
      "cursor: pointer",
      "animation: __upd_pop 0.18s ease-out",
    ].join(";");
    toast.addEventListener("click", () => {
      toast.disabled = true;
      toast.textContent = "Refreshing…";
      location.reload();
    });
    if (!document.getElementById("__upd_style__")) {
      const s = document.createElement("style");
      s.id = "__upd_style__";
      s.textContent =
        "@keyframes __upd_pop { from { opacity: 0; transform: translate(-50%, 8px) } to { opacity: 1; transform: translate(-50%, 0) } }";
      document.head.appendChild(s);
    }
    document.body.appendChild(toast);
  }

  function showHint(text) {
    let hint = document.getElementById("__install_hint__");
    if (!hint) {
      hint = document.createElement("div");
      hint.id = "__install_hint__";
      hint.style.cssText = [
        "position: fixed",
        "left: 50%",
        "bottom: 24px",
        "transform: translateX(-50%)",
        "z-index: 3000",
        "padding: 10px 14px",
        "background: #26a69a",
        "color: #0f1218",
        "border-radius: 8px",
        "font: 600 13px/1 system-ui, -apple-system, Segoe UI, Roboto, sans-serif",
        "box-shadow: 0 4px 14px rgba(0,0,0,0.4)",
        "pointer-events: none",
      ].join(";");
      document.body.appendChild(hint);
    }
    hint.textContent = text;
    hint.style.opacity = "1";
    clearTimeout(showHint._t);
    showHint._t = setTimeout(() => { hint.style.opacity = "0"; }, 3500);
  }

  // Mobile drawer toggle: collapse/expand the side pane. Only meaningful in
  // the mobile layout (< 900px); on desktop the toggle is display:none.
  const toggle = document.getElementById("drawer-toggle");
  const pane = document.getElementById("side-pane");
  if (toggle && pane) {
    const setCollapsed = (v) => {
      pane.dataset.collapsed = v ? "true" : "false";
      toggle.setAttribute("aria-expanded", v ? "false" : "true");
    };
    // Start collapsed so the chart owns the whole viewport on first paint.
    setCollapsed(true);
    toggle.addEventListener("click", () => {
      setCollapsed(pane.dataset.collapsed !== "true" ? true : false);
    });
  }

  // Mobile drawer tabs: Trade / Tape / History. Only one panel shows at a
  // time so each gets the full drawer body. The CSS uses
  // .side-pane[data-tab="…"] to pick which child to render.
  const tabs = document.querySelectorAll("#drawer-tabs button[data-tab]");
  if (tabs.length && pane) {
    tabs.forEach((btn) => {
      btn.addEventListener("click", () => {
        pane.dataset.tab = btn.dataset.tab;
        tabs.forEach((t) => {
          const on = t === btn;
          t.classList.toggle("active", on);
          t.setAttribute("aria-selected", on ? "true" : "false");
        });
      });
    });
  }

  // Mobile collapsible rows — Tools (chart-toolbar) and Indicators. Tapping
  // the trigger toggles the row's .open class; tapping anywhere outside the
  // row closes it. The hide/show is pure CSS — see style.css mobile block.
  document.querySelectorAll(".mobile-toggle").forEach((btn) => {
    const row = btn.parentElement;
    if (!row) return;
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const willOpen = !row.classList.contains("open");
      // Close any other open mobile row first so only one is expanded.
      document
        .querySelectorAll(".chart-toolbar.open, .indicators.open")
        .forEach((r) => { if (r !== row) r.classList.remove("open"); });
      row.classList.toggle("open", willOpen);
      btn.setAttribute("aria-expanded", willOpen ? "true" : "false");
    });
  });
  document.addEventListener("click", (e) => {
    document
      .querySelectorAll(".chart-toolbar.open, .indicators.open")
      .forEach((row) => {
        if (!row.contains(e.target)) {
          row.classList.remove("open");
          const t = row.querySelector(".mobile-toggle");
          if (t) t.setAttribute("aria-expanded", "false");
        }
      });
  });
})();
