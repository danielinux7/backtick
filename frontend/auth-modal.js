// Login / Sign up modal — opened from the header "Login / Sign up" link.
// Same flow as /login but stays on the chart page; on success, the modal
// closes and the header re-renders via a small custom event so app.js can
// pull /api/auth/me again and update the email + Logout area.
(function () {
  const modal = document.getElementById("auth-modal");
  const closeBtn = document.getElementById("auth-modal-close");
  if (!modal || !closeBtn) return;

  const form = document.getElementById("m-form");
  const email = document.getElementById("m-email");
  const password = document.getElementById("m-password");
  const submit = document.getElementById("m-submit");
  const errEl = document.getElementById("m-err");
  const title = document.getElementById("m-title");
  const subtitle = document.getElementById("m-subtitle");
  const togglePrompt = document.getElementById("m-toggle-prompt");
  const toggleLink = document.getElementById("m-toggle-link");

  let mode = "login";          // "login" | "register"
  let isGuest = false;         // populated each time the modal opens
  let lastFocus = null;
  let providers = null;        // {google, apple} — fetched once, then cached

  // Hide OAuth buttons (and the "or" divider) for providers the server hasn't
  // configured, so we never show a button that just 503s. Email auth always
  // stays. Cached after the first fetch.
  async function applyProviders() {
    if (!providers) {
      providers = await fetch("/api/auth/providers", { credentials: "include" })
        .then((r) => (r.ok ? r.json() : {}))
        .catch(() => ({}));
    }
    const g = document.querySelector("#auth-modal .google-btn");
    const a = document.querySelector("#auth-modal .apple-btn");
    if (g) g.hidden = !providers.google;
    if (a) a.hidden = !providers.apple;
    const div = document.getElementById("m-oauth-divider");
    if (div) div.hidden = !(providers.google || providers.apple);
  }

  function render() {
    if (mode === "login") {
      title.textContent = "Sign in";
      subtitle.textContent = "Welcome back. Pick up where you left off.";
      submit.textContent = "Sign in";
      password.setAttribute("autocomplete", "current-password");
      togglePrompt.textContent = "No account?";
      toggleLink.textContent = "Create one";
    } else {
      title.textContent = isGuest ? "Save your account" : "Create account";
      subtitle.textContent = isGuest
        ? "Add an email + password to keep your trades + watchlist past this browser."
        : "Start with email + password. You can link Google later.";
      submit.textContent = isGuest ? "Save account" : "Create account";
      password.setAttribute("autocomplete", "new-password");
      togglePrompt.textContent = "Already have an account?";
      toggleLink.textContent = "Sign in";
    }
  }

  async function refreshGuestFlag() {
    try {
      const r = await fetch("/api/auth/me", { credentials: "include" });
      if (r.ok) {
        const me = await r.json();
        isGuest = !!me.is_guest;
      } else {
        isGuest = false;
      }
    } catch (_) { isGuest = false; }
  }

  async function open(initialMode = "login") {
    await refreshGuestFlag();
    await applyProviders();
    mode = initialMode;
    errEl.textContent = "";
    email.value = "";
    password.value = "";
    render();
    lastFocus = document.activeElement;
    modal.hidden = false;
    document.body.style.overflow = "hidden";
    // Focus the email field for keyboard-first users
    setTimeout(() => email.focus(), 0);
  }

  function close() {
    modal.hidden = true;
    document.body.style.overflow = "";
    if (lastFocus && typeof lastFocus.focus === "function") lastFocus.focus();
  }

  // Click handlers
  closeBtn.addEventListener("click", close);
  modal.addEventListener("click", (e) => { if (e.target === modal) close(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !modal.hidden) close();
  });

  toggleLink.addEventListener("click", (e) => {
    e.preventDefault();
    mode = mode === "login" ? "register" : "login";
    errEl.textContent = "";
    render();
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    errEl.textContent = "";
    submit.disabled = true;
    try {
      // Register-from-guest silently routes to /upgrade to keep the user_id.
      const effective = mode === "register" && isGuest ? "upgrade" : mode;
      const path =
        effective === "login" ? "/api/auth/login" :
        effective === "register" ? "/api/auth/register" :
        "/api/auth/upgrade";
      const r = await fetch(path, {
        method: "POST",
        credentials: "include",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ email: email.value, password: password.value }),
      });
      if (!r.ok) {
        let msg = await r.text();
        try { msg = JSON.parse(msg).detail || msg; } catch (_) {}
        throw new Error(msg);
      }
      close();
      // Tell the rest of the app to refresh the header (and anything else
      // that cares about the auth state).
      window.dispatchEvent(new CustomEvent("auth:changed"));
    } catch (err) {
      errEl.textContent = err.message || "Something went wrong";
    } finally {
      submit.disabled = false;
    }
  });

  // Expose a tiny API for app.js to wire up the header link.
  window.openAuthModal = open;
})();
