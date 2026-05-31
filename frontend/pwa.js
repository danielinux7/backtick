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

  // Mobile drawer tabs (Tape / History) double as the expand/collapse control:
  // the tab strip is always visible; tapping a tab opens the drawer to that
  // panel, tapping the active tab again collapses it. Only meaningful in the
  // mobile layout (< 900px); on desktop the strip is display:none and all
  // panels stack. The CSS uses .side-pane[data-tab="…"] to pick the panel and
  // [data-collapsed] for the height.
  const pane = document.getElementById("side-pane");
  const tabs = document.querySelectorAll("#drawer-tabs button[data-tab]");
  if (tabs.length && pane) {
    // Remember the user's dragged height; reapply on expand. Collapse clears the
    // inline height so the CSS collapsed-height (40px) wins.
    let drawerHeight = "";
    const setCollapsed = (v) => {
      pane.dataset.collapsed = v ? "true" : "false";
      pane.style.height = v ? "" : drawerHeight;
    };
    const selectTab = (name) => {
      pane.dataset.tab = name;
      tabs.forEach((t) => {
        const on = t.dataset.tab === name;
        t.classList.toggle("active", on);
        t.setAttribute("aria-selected", on ? "true" : "false");
      });
    };
    // Start collapsed so the chart owns the whole viewport on first paint.
    setCollapsed(true);
    // A drag that started on the tab strip must not also toggle the tab on the
    // trailing click. A sticky one-shot flag was unreliable on touch (a drag
    // often fires NO trailing click, so the flag lingered and ate the user's
    // next real tap — the "tap twice" bug). Instead, ignore only a click that
    // lands within a short window after a drag ends.
    let lastDragEndAt = 0;
    tabs.forEach((btn) => {
      btn.addEventListener("click", () => {
        if (Date.now() - lastDragEndAt < 350) return;   // swallow the drag's trailing click
        const collapsed = pane.dataset.collapsed === "true";
        if (collapsed) {                       // closed → open to this tab
          selectTab(btn.dataset.tab);
          setCollapsed(false);
        } else if (pane.dataset.tab === btn.dataset.tab) {
          setCollapsed(true);                  // tapping the open tab → collapse
        } else {
          selectTab(btn.dataset.tab);          // switch tab, stay open
        }
      });
    });

    // Drag the top edge of the drawer to resize it (mobile). Mirrors the
    // RSI/CVD pane-resize handles: pointer events so touch works. The whole
    // drawer header is grabbable — the dedicated handle AND the tab strip — so
    // it's easy to land on. On the tab strip we use a small move threshold so a
    // tap still switches tabs while a drag resizes.
    const tabStrip = document.getElementById("drawer-tabs");
    // The drawer is anchored to the bottom of the viewport and grows upward, so
    // its bottom edge is fixed. Sizing from `bottom - pointerY` (rather than a
    // delta off a start height that changes the moment we un-collapse) keeps the
    // top edge glued to the finger with no jitter.
    const beginDrawerDrag = (e, el, threshold = 0) => {
      const startY = e.clientY;
      const paneBottom = pane.getBoundingClientRect().bottom;   // stable while dragging
      let dragging = threshold === 0;
      const resizeTo = (clientY) => {
        const h = Math.max(40, Math.min(window.innerHeight * 0.85, paneBottom - clientY));
        drawerHeight = h + "px";
        pane.style.height = drawerHeight;
      };
      if (dragging) {
        e.preventDefault();
        try { el.setPointerCapture(e.pointerId); } catch (_) {}
        if (pane.dataset.collapsed === "true") setCollapsed(false);
        resizeTo(startY);
      }
      const onMove = (ev) => {
        if (!dragging) {
          if (Math.abs(startY - ev.clientY) < threshold) return;
          dragging = true;
          try { el.setPointerCapture(ev.pointerId); } catch (_) {}
          if (pane.dataset.collapsed === "true") setCollapsed(false);
        }
        resizeTo(ev.clientY);
      };
      const onUp = () => {
        el.removeEventListener("pointermove", onMove);
        el.removeEventListener("pointerup", onUp);
        el.removeEventListener("pointercancel", onUp);
        if (dragging && threshold > 0) lastDragEndAt = Date.now();
      };
      el.addEventListener("pointermove", onMove);
      el.addEventListener("pointerup", onUp);
      el.addEventListener("pointercancel", onUp);
    };

    const handle = document.getElementById("drawer-resize");
    if (handle) handle.addEventListener("pointerdown", (e) => beginDrawerDrag(e, handle));
    if (tabStrip) tabStrip.addEventListener("pointerdown", (e) => beginDrawerDrag(e, tabStrip, 6));
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
