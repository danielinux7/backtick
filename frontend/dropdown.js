// Custom dropdown: replaces the browser's native <select> option list (which is
// unstyled/"crude" on mobile and leaves a lingering focus ring) with a styled
// popover. The native <select> is kept in the DOM as the source of truth — its
// value still posts via FormData and existing `change` listeners (mode-select in
// app.js, market in symbol-picker.js) keep firing — we just hide it and drive it
// from the popover. Mirrors the popover pattern in symbol-picker.js.
(function () {
  function enhanceSelect(sel) {
    if (!sel || sel.dataset.ddEnhanced) return;
    sel.dataset.ddEnhanced = "1";

    const wrap = document.createElement("div");
    wrap.className = "dd";
    // data-dropup opens the popover upward (for selects near the bottom edge,
    // e.g. the replay-speed control sitting above the fixed mobile trade bar).
    if (sel.hasAttribute("data-dropup")) wrap.classList.add("dd-up");
    sel.parentNode.insertBefore(wrap, sel);
    wrap.appendChild(sel);
    sel.classList.add("dd-native");   // hidden, but still submitted in FormData

    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "dd-trigger";
    trigger.setAttribute("aria-haspopup", "listbox");
    trigger.setAttribute("aria-expanded", "false");
    if (sel.title) trigger.title = sel.title;
    const label = document.createElement("span");
    label.className = "dd-label";
    trigger.appendChild(label);
    wrap.appendChild(trigger);

    const pop = document.createElement("div");
    pop.className = "dd-popover";
    pop.setAttribute("role", "listbox");
    pop.hidden = true;
    wrap.appendChild(pop);

    const syncLabel = () => {
      const opt = sel.options[sel.selectedIndex];
      label.textContent = opt ? opt.textContent : "";
    };

    // Build rows lazily on every open so dynamically-populated options
    // (e.g. #tf-select, filled by app.js) are always reflected.
    const buildRows = () => {
      pop.textContent = "";
      Array.from(sel.options).forEach((opt) => {
        const row = document.createElement("button");
        row.type = "button";
        row.className = "dd-row";
        row.setAttribute("role", "option");
        if (opt.value === sel.value) {
          row.classList.add("active");
          row.setAttribute("aria-selected", "true");
        }
        row.textContent = opt.textContent;
        row.addEventListener("click", () => {
          if (sel.value !== opt.value) {
            sel.value = opt.value;
            sel.dispatchEvent(new Event("change", { bubbles: true }));
          }
          syncLabel();
          close();
        });
        pop.appendChild(row);
      });
    };

    const open = () => {
      buildRows();
      pop.hidden = false;
      trigger.setAttribute("aria-expanded", "true");
    };
    const close = () => {
      pop.hidden = true;
      trigger.setAttribute("aria-expanded", "false");
      trigger.blur();   // drop the focus ring after a choice
    };

    trigger.addEventListener("click", (e) => {
      e.preventDefault();
      if (pop.hidden) open(); else close();
    });
    // reflect programmatic changes (e.g. symbol-picker setting the market) on the trigger
    sel.addEventListener("change", syncLabel);
    // …and re-sync when the options themselves are rebuilt without a change event
    // (e.g. #speed is repopulated when the Tick/Candle replay mode switches).
    new MutationObserver(syncLabel).observe(sel, { childList: true });
    document.addEventListener("click", (e) => {
      if (!pop.hidden && !wrap.contains(e.target)) close();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !pop.hidden) close();
    });

    syncLabel();
  }

  function init() {
    // #mode-select is intentionally excluded — it's driven by the segmented
    // Live/Replay toggle in the header and kept hidden as the form value.
    ['select[name="market"]', "#tf-select", "#speed"].forEach((q) => {
      enhanceSelect(document.querySelector(q));
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
