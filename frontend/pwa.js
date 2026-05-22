// Service worker registration + Android Chrome's beforeinstallprompt.
// iOS Safari doesn't fire beforeinstallprompt — users install via the Share
// sheet manually; we surface a one-time hint there instead.

(function () {
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch((err) => {
        console.warn("SW registration failed:", err);
      });
    });
  }

  let deferredPrompt = null;
  const btn = document.getElementById("install-btn");
  if (!btn) return;

  // Position the floating install button bottom-right.
  btn.style.cssText = [
    "position: fixed",
    "bottom: 16px",
    "right: 16px",
    "z-index: 999",
    "padding: 10px 14px",
    "background: #26a69a",
    "color: #0f1218",
    "border: 0",
    "border-radius: 8px",
    "font: 600 13px/1 system-ui, -apple-system, Segoe UI, Roboto, sans-serif",
    "box-shadow: 0 4px 14px rgba(0,0,0,0.4)",
    "cursor: pointer",
  ].join(";");

  // Android Chrome / Edge.
  window.addEventListener("beforeinstallprompt", (e) => {
    e.preventDefault();
    deferredPrompt = e;
    btn.hidden = false;
  });

  btn.addEventListener("click", async () => {
    if (deferredPrompt) {
      btn.disabled = true;
      try {
        deferredPrompt.prompt();
        await deferredPrompt.userChoice;
      } finally {
        deferredPrompt = null;
        btn.hidden = true;
        btn.disabled = false;
      }
      return;
    }
    // iOS / unsupported: show a brief hint.
    const ua = navigator.userAgent || "";
    const isIOS = /iPad|iPhone|iPod/.test(ua) && !window.MSStream;
    btn.disabled = true;
    btn.textContent = isIOS
      ? "Share → Add to Home Screen"
      : "Use your browser menu to install";
    setTimeout(() => {
      btn.disabled = false;
      btn.textContent = "Install app";
      btn.hidden = true;
    }, 4000);
  });

  // Hide the button when already running as a standalone PWA.
  const standalone =
    window.matchMedia("(display-mode: standalone)").matches ||
    window.navigator.standalone === true;
  if (standalone) btn.hidden = true;

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
})();
